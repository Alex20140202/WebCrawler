from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

# Text that marks "there is more to fetch" in the DOM.
NEXT_TEXT = re.compile(
    r"\b(next|next\s*page|more|load\s*more|show\s*more|older|继续|下一页|更多)\b",
    re.IGNORECASE,
)

PAGINATOR_CLASS = re.compile(
    r"(pagination|paginator|page-nav|page-numbers|page-menu|nav-links|paging)", re.IGNORECASE
)

LOGIN_HINT = re.compile(
    r"(sign\s*in|log\s*in|login|authenticate|member\s*area|signin|sign-in)",
    re.IGNORECASE,
)

SEARCH_FORM_HINT = re.compile(r"(search|query|q=|find|keyword|suche)", re.IGNORECASE)

FEED_RELS = frozenset({"alternate"})
FEED_TYPES = re.compile(r"(rss|atom|application/feed\+json|json)", re.IGNORECASE)

API_HINT = re.compile(
    r"(/api/|/graphql|\.json$|/rest/|/wp-json/|/ajax)", re.IGNORECASE
)


class Finding:
    """Something the crawler noticed about a page and may want to act on."""

    __slots__ = ("kind", "url", "detail", "meta")

    def __init__(self, kind: str, url: str = "", detail: str = "", meta: Optional[Dict] = None):
        self.kind = kind
        self.url = url
        self.detail = detail
        self.meta = meta or {}

    def as_dict(self) -> Dict:
        return {"kind": self.kind, "url": self.url, "detail": self.detail,
                "meta": self.meta}

    def __repr__(self):
        return "Finding(%s, %s)" % (self.kind, self.url or self.detail)


class PageSignals:
    """Everything we learned from one page, without deciding what to do yet."""

    __slots__ = (
        "url", "needs_login", "pagination_urls", "next_urls", "feed_urls",
        "api_urls", "get_forms", "js_rendered", "blocked_by_cookie_wall",
    )

    def __init__(self, url: str):
        self.url = url
        self.needs_login = False
        self.pagination_urls: List[str] = []
        self.next_urls: List[str] = []
        self.feed_urls: List[str] = []
        self.api_urls: List[str] = []
        self.get_forms: List[Dict] = []
        self.js_rendered = False
        self.blocked_by_cookie_wall = False

    def findings(self) -> List[Finding]:
        out: List[Finding] = []
        if self.needs_login:
            out.append(Finding(
                "login_required", self.url,
                "page appears to require signing in",
            ))
        if self.blocked_by_cookie_wall:
            out.append(Finding(
                "cookie_wall", self.url,
                "cookie consent wall in the way",
            ))
        if self.pagination_urls:
            out.append(Finding(
                "pagination", self.url,
                "%d paginated link(s)" % len(self.pagination_urls),
                {"urls": self.pagination_urls[:10]},
            ))
        if self.next_urls:
            out.append(Finding(
                "next_page", self.url,
                "%d next-page link(s)" % len(self.next_urls),
                {"urls": self.next_urls[:10]},
            ))
        if self.feed_urls:
            out.append(Finding(
                "feed", self.url,
                "%d feed link(s)" % len(self.feed_urls),
                {"urls": self.feed_urls[:10]},
            ))
        if self.api_urls:
            out.append(Finding(
                "api_endpoint", self.url,
                "%d API-looking URL(s)" % len(self.api_urls),
                {"urls": self.api_urls[:10]},
            ))
        if self.get_forms:
            out.append(Finding(
                "search_form", self.url,
                "%d GET search form(s)" % len(self.get_forms),
                {"forms": self.get_forms[:5]},
            ))
        if self.js_rendered:
            out.append(Finding(
                "js_rendered", self.url,
                "content looks client-side rendered",
            ))
        return out


def _abs(base: str, href: str) -> Optional[str]:
    from .parser import normalize_url
    return normalize_url(href, base)


def scan_page(html: str, url: str, word_count: int = 0, link_count: int = 0) -> PageSignals:
    """Look for follow-up work a human would click: next pages, feeds, search forms."""
    signals = PageSignals(url)
    if not html:
        return signals

    soup = BeautifulSoup(html, "html.parser")

    # Feed and alternate links in <head>: cheap extra URLs, often the whole site.
    for link in soup.find_all("link", href=True):
        rels = [str(r).lower() for r in (link.get("rel") or [])]
        link_type = str(link.get("type") or "")
        href = _abs(url, link["href"])
        if href is None:
            continue
        if FEED_RELS.intersection(rels) and FEED_TYPES.search(link_type):
            if href not in signals.feed_urls:
                signals.feed_urls.append(href)

    # Password field or sign-in wording. Login itself is handled once up front
    # by the authenticator; here we only note that the page is gated so the
    # crawler does not treat its links as freely reachable content.
    if soup.find("input", {"type": "password"}):
        signals.needs_login = True
    if soup.find("form", {"action": re.compile(r"(login|signin|sign-in|auth)",
                                               re.IGNORECASE)}):
        if soup.find("input", {"type": "password"}):
            signals.needs_login = True

    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = _abs(url, anchor["href"])
        if href is None:
            continue

        if NEXT_TEXT.search(text) and len(text) <= 40:
            if href not in signals.next_urls:
                signals.next_urls.append(href)
            continue

        if LOGIN_HINT.search(text) and len(text) <= 30:
            signals.needs_login = True

        if API_HINT.search(href):
            if href not in signals.api_urls:
                signals.api_urls.append(href)

    # Pagination: an element whose class/id looks like a pager, or numeric links.
    for node in soup.find_all(attrs={"class": PAGINATOR_CLASS}):
        for anchor in node.find_all("a", href=True):
            href = _abs(url, anchor["href"])
            if href and href not in signals.pagination_urls:
                signals.pagination_urls.append(href)
    for node in soup.find_all(attrs={"id": PAGINATOR_CLASS}):
        for anchor in node.find_all("a", href=True):
            href = _abs(url, anchor["href"])
            if href and href not in signals.pagination_urls:
                signals.pagination_urls.append(href)

    # "Load more" style buttons usually point at a JSON endpoint.
    for node in soup.find_all(["button", "a", "div", "span"]):
        text = node.get_text(" ", strip=True)
        if not text or len(text) > 30:
            continue
        if not NEXT_TEXT.search(text):
            continue
        for attribute in ("data-url", "data-href", "data-next", "data-load-more",
                          "data-endpoint", "data-src"):
            raw = node.get(attribute)
            href = _abs(url, raw) if raw else None
            if href:
                if href not in signals.api_urls:
                    signals.api_urls.append(href)

    # GET forms are safe to enumerate; anything with POST is left alone.
    for form in soup.find_all("form"):
        method = str(form.get("method") or "get").lower()
        if method != "get":
            continue
        action = form.get("action") or ""
        fields = []
        for field in form.find_all(["input", "select"]):
            name = field.get("name")
            if not name:
                continue
            field_type = str(field.get("type") or "text").lower()
            if field_type in ("submit", "button", "image"):
                continue
            fields.append(name)
        if not fields:
            continue
        haystack = " ".join([action, " ".join(fields),
                             form.get_text(" ", strip=True)[:120]])
        if not SEARCH_FORM_HINT.search(haystack):
            continue
        target = _abs(url, action) if action else url
        if target is None:
            continue
        signals.get_forms.append({
            "action": target,
            "fields": fields,
            "field": fields[0],
        })

    # A cookie wall hides content behind consent; note it so we can retry later.
    for node in soup.find_all(["div", "section", "dialog"]):
        marker = " ".join([
            " ".join(node.get("class") or []),
            str(node.get("id") or ""),
        ]).lower()
        if "cookie" in marker and ("banner" in marker or "consent" in marker
                                   or "wall" in marker or "notice" in marker):
            signals.blocked_by_cookie_wall = True
            break

    scripts = len(re.findall(r"<script\b", html, re.IGNORECASE))
    if scripts >= 3 and word_count <= 50 and link_count <= 5:
        signals.js_rendered = True

    return signals


def build_paged_urls(base_url: str, template_url: str, limit: int = 20
                     ) -> List[str]:
    """Generate further paginated URLs by inferring the pattern from a known one.

    Works for ?page=2, ?start=10 and /page/2 shapes because the pattern is read
    off the observed link instead of assumed. `limit` caps how many URLs come
    back; the walk starts at the observed page and moves upward, so we never
    re-request page 1 or descend into an offset scheme where 0 is the first page.
    """
    from .parser import normalize_url

    base_parts = urlsplit(base_url)
    target_parts = urlsplit(template_url)

    candidates: List[str] = []
    seen = set()

    query_pairs = parse_qsl(target_parts.query, keep_blank_values=True)
    if query_pairs:
        key = None
        for name, value in query_pairs:
            if str(value).isdigit() and int(value) >= 2:
                key = name
                break
        if key:
            observed = next(int(value) for name, value in query_pairs
                            if name == key)
            # Walk upward from what we actually observed. Guessing downwards can
            # re-fetch page 1 or land inside an offset scheme where 0 is first.
            number = observed if observed > 2 else 2
            while len(candidates) < limit:
                pairs = [(name, str(number) if name == key else value)
                         for name, value in query_pairs]
                url = normalize_url(urlunsplit(
                    (target_parts.scheme, target_parts.netloc, target_parts.path,
                     urlencode(pairs), "")))
                number += 1
                if url and url not in seen and url != template_url:
                    seen.add(url)
                    candidates.append(url)
            return candidates

    path_match = re.search(r"/(page|p|paged|pg)/(\d+)", target_parts.path, re.IGNORECASE)
    if path_match:
        prefix = path_match.group(1)
        number = max(int(path_match.group(2)), 2)
        while len(candidates) < limit:
            path = target_parts.path[:path_match.start()] + "/%s/%d" % (prefix, number)
            url = normalize_url(urlunsplit((target_parts.scheme, target_parts.netloc,
                                            path, target_parts.query, "")))
            number += 1
            if url and url not in seen and url != template_url:
                seen.add(url)
                candidates.append(url)
        return candidates

    return candidates


def expand_search_form(form: Dict, terms: List[str]) -> List[str]:
    """Build one GET URL per search term. Read-only: it only ever fetches."""
    from .parser import normalize_url

    parts = urlsplit(form["action"])
    skip = form.get("field")
    out = []
    seen = set()
    for term in terms:
        query = [(name, term if name == skip else value)
                 for name, value in parse_qsl(parts.query, keep_blank_values=True)]
        if skip and not any(name == skip for name, _ in query):
            query.append((skip, term))
        url = normalize_url(urlunsplit((parts.scheme, parts.netloc, parts.path,
                                        urlencode(query), "")))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out