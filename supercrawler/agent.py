from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from .discovery import is_crawlable_url, strip_tracking_params
from .interact import Interactor
from .parser import normalize_url, same_site
from .scanner import Finding, build_paged_urls, expand_search_form

Logger = Optional[Callable[[str], None]]

# Actions are read-only: every one of them only ever queues URLs to fetch.
# Nothing here submits a form, posts data, or authenticates.
ACTION_PAGINATE = "expand_pagination"
ACTION_NEXT = "follow_next_links"
ACTION_FEED = "fetch_feeds"
ACTION_SEARCH = "run_searches"
ACTION_LOGIN = "skip_login_walled"
ACTION_JS = "note_js_rendered"
ACTION_API = "skip_api_endpoints"


class ActionResult:
    __slots__ = ("action", "urls", "skipped", "reason")

    def __init__(self, action: str, urls: Optional[List[str]] = None,
                 skipped: bool = False, reason: str = ""):
        self.action = action
        self.urls = urls or []
        self.skipped = skipped
        self.reason = reason

    def as_dict(self) -> Dict:
        return {"action": self.action, "added": len(self.urls),
                "skipped": self.skipped, "reason": self.reason}


class Agent:
    """Turns page observations into queued URLs, asking the user when unsure.

    The contract is deliberately narrow: this may add URLs to the crawl queue and
    it may report things it cannot do. It will not submit forms, follow POST
    links, or attempt to authenticate.
    """

    MAX_PAGINATION_PAGES = 20
    MAX_SEARCH_TERMS = 5

    def __init__(
        self,
        config,
        interactor: Optional[Interactor] = None,
        logger: Logger = None,
        max_actions_per_page: Optional[int] = None,
        in_scope: Optional[Callable[[str], bool]] = None,
    ):
        self.config = config
        self.interactor = interactor or Interactor(mode="never", logger=logger)
        self.logger = logger
        if max_actions_per_page is None:
            max_actions_per_page = getattr(config, "max_actions_per_page", 25)
        self.max_actions_per_page = max(0, max_actions_per_page)
        self._scope_check = in_scope
        self.actions_taken: List[Dict] = []
        self.asked_topics: List[str] = []

    def _log(self, message: str) -> None:
        if self.logger and self.config.verbose:
            self.logger(message)

    def consider(self, url: str, signals, depth: int) -> List[str]:
        """Decide what to do about one page and return URLs worth fetching."""
        if not getattr(self.config, "auto_actions", True):
            return []

        findings = signals.findings() if signals else []
        queued: List[str] = []
        budget = self.max_actions_per_page

        for finding in findings:
            if budget <= 0:
                self._log("  ! action budget spent on this page")
                break
            result = self._handle(finding, signals, depth)
            self.actions_taken.append(result.as_dict())
            if result.urls:
                queued.extend(result.urls)
                budget -= len(result.urls)

        cleaned = self._clean(queued, url)
        if len(cleaned) > self.max_actions_per_page:
            self._log("  ! capping %d discovered URL(s) at %d for this page"
                      % (len(cleaned), self.max_actions_per_page))
            cleaned = cleaned[:self.max_actions_per_page]
        if cleaned:
            self._log("  ~ found %d extra URL(s) worth crawling" % len(cleaned))
        return cleaned

    def _handle(self, finding: Finding, signals, depth: int) -> ActionResult:
        if finding.kind == "pagination":
            return self._on_pagination(finding, signals, depth)
        if finding.kind == "next_page":
            return self._on_next(finding, signals, depth)
        if finding.kind == "feed":
            return self._on_feed(finding, depth)
        if finding.kind == "search_form":
            return self._on_search(finding, depth)
        if finding.kind == "login_required":
            return self._on_login(finding)
        if finding.kind == "js_rendered":
            return self._on_js(finding)
        if finding.kind == "cookie_wall":
            return ActionResult("note_cookie_wall", [], False,
                                "content may be behind a consent dialog")
        if finding.kind == "api_endpoint":
            return ActionResult(ACTION_API, [], False,
                                "API URLs need a browser or a JSON parser")
        return ActionResult("ignored", [], False, finding.kind)

    def _on_pagination(self, finding: Finding, signals, depth: int) -> ActionResult:
        if not signals.pagination_urls:
            return ActionResult(ACTION_PAGINATE, [], False, "no pagination links")

        pages = self.interactor.get(
            "pagination_depth",
            "How many extra paginated pages should I fetch from listings?",
            self.MAX_PAGINATION_PAGES,
            "%s looks paginated" % finding.url,
        )
        limit = max(0, min(int(pages.value or 0), 200))
        if limit == 0:
            return ActionResult(ACTION_PAGINATE, [], True, "declined by user")

        # The first link in a pager is usually "page 1", which reveals no
        # pattern. Try each link until one actually implies a page sequence.
        for template in signals.pagination_urls:
            urls = build_paged_urls(finding.url, template, limit=limit)
            if urls:
                self.asked_topics.append("pagination_depth")
                return ActionResult(ACTION_PAGINATE, urls, False,
                                    "expanding %s" % template)

        self.asked_topics.append("pagination_depth")
        return ActionResult(ACTION_PAGINATE, [], False,
                            "pager links do not reveal a page pattern")

    def _on_next(self, finding: Finding, signals, depth: int) -> ActionResult:
        choice = self.interactor.get(
            "follow_next",
            "Follow 'next page' links found while crawling?",
            True,
            "these lead to more content in the same section",
        )
        if not choice.value:
            return ActionResult(ACTION_NEXT, [], True, "declined by user")
        self.asked_topics.append("follow_next")
        return ActionResult(ACTION_NEXT, list(signals.next_urls[:5]), False,
                            "%d next link(s)" % len(signals.next_urls[:5]))

    def _on_feed(self, finding: Finding, depth: int) -> ActionResult:
        choice = self.interactor.get(
            "fetch_feeds",
            "Follow RSS/Atom feed links? They often list pages nothing links to.",
            False,
            "found %d feed link(s)" % len(finding.meta.get("urls", [])),
        )
        if not choice.value:
            return ActionResult(ACTION_FEED, [], True, "declined or default off")
        self.asked_topics.append("fetch_feeds")
        return ActionResult(ACTION_FEED, list(finding.meta.get("urls", []))[:5],
                            False, "following feed links")

    def _on_search(self, finding: Finding, depth: int) -> ActionResult:
        answer = self.interactor.get(
            "search_terms",
            "Type search terms to run against this site's own search box "
            "(comma separated, blank to skip).",
            "",
            "GET form on %s" % finding.url,
        )
        raw = str(answer.value or "").strip()
        self.asked_topics.append("search_terms")
        if not raw:
            return ActionResult(ACTION_SEARCH, [], True, "no search terms given")

        terms = [t.strip() for t in raw.split(",") if t.strip()]
        terms = terms[: self.MAX_SEARCH_TERMS]
        urls: List[str] = []
        for form in finding.meta.get("forms", []):
            urls.extend(expand_search_form(form, terms))
        return ActionResult(ACTION_SEARCH, urls, False,
                            "%d term(s) across %d form(s)"
                            % (len(terms), len(finding.meta.get("forms", []))))

    def _on_login(self, finding: Finding) -> ActionResult:
        """Never attempt to authenticate; just record the wall."""
        self._log("  ! %s needs a login; skipping (this crawler does not sign in)"
                  % finding.url)
        self.interactor.get(
            "login_walls",
            "Some pages need a login. I will not sign in; continue past them?",
            True,
            "%s appears gated" % finding.url,
        )
        self.asked_topics.append("login_walls")
        return ActionResult(ACTION_LOGIN, [], True, "authentication not attempted")

    def _on_js(self, finding: Finding) -> ActionResult:
        return ActionResult(ACTION_JS, [], True,
                            "needs a JavaScript-capable browser to read")

    def _clean(self, urls: Sequence[str], base_url: str) -> List[str]:
        out: List[str] = []
        for raw in urls:
            url = normalize_url(raw)
            if not url:
                continue
            if self.config.strip_params:
                url = normalize_url(strip_tracking_params(url)) or url
            if url in out:
                continue
            if not is_crawlable_url(url, allow_search=True):
                continue
            if not same_site(url, base_url) and not self.config.follow_external:
                continue
            if not self._in_scope(url):
                continue
            out.append(url)
        return out

    def _in_scope(self, url: str) -> bool:
        if self._scope_check is not None:
            return self._scope_check(url)
        return True

    def summary(self) -> Dict:
        counts: Dict[str, int] = {}
        for item in self.actions_taken:
            counts[item["action"]] = counts.get(item["action"], 0) + 1
        return {
            "actions_total": len(self.actions_taken),
            "actions_by_kind": counts,
            "topics_consulted": sorted(set(self.asked_topics)),
            "interactions": self.interactor.summary(),
        }