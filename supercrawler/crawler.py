from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from .config import CrawlConfig
from .agent import Agent
from .auth import describe_auth
from .discovery import (
    estimate_from_links,
    is_crawlable_url,
    looks_js_rendered,
    probe_site,
    recommend_presets,
    strip_tracking_params,
)
from .fetcher import Fetcher
from .interact import Interactor
from .parser import (
    extract_emails,
    extract_links,
    extract_page,
    normalize_url,
    registrable_domain,
    word_frequencies,
)
from .scanner import scan_page
from .state import CrawlState, load_state, save_state

Logger = Optional[Callable[[str], None]]


class Crawler:
    """Breadth-first, rate-limited, robots-aware web crawler."""

    def __init__(self, config: CrawlConfig, logger: Logger = None,
                 interactor: Optional[Interactor] = None):
        self.config = config
        self.config.validate()
        self.logger = logger
        self._seen = set()
        self._lock = threading.Lock()
        self._pages: List[Dict] = []
        self._counts = {"skipped": 0, "robots_blocked": 0, "errors": 0, "duplicates": 0}
        self._content_hashes: Dict[str, str] = {}
        self._state = CrawlState()
        self.probe = None
        self.total_known = 0
        self._sitemap_urls: Set[str] = set()
        self._js_heavy_pages = 0
        self._auth_result = None
        self.interactor = interactor or Interactor(
            mode=config.interaction_mode,
            max_questions=config.max_questions,
            logger=logger,
        )
        self.agent = Agent(
            config, interactor=self.interactor, logger=logger,
            in_scope=self._in_scope,
        )

    def _log(self, message: str) -> None:
        if self.logger and self.config.verbose:
            self.logger(message)

    def plan(self) -> Dict:
        """Probe the target and return a crawl plan without fetching pages."""
        base = self._seed_url(self.config.seeds[0]) if self.config.seeds else None
        if not base:
            raise ValueError("no valid seed URL")

        def fetch_text(url: str) -> Optional[str]:
            with Fetcher(self.config) as fetcher:
                result = fetcher.get(url)
            return result.text if result and result.ok else None

        self._log("probing %s for sitemaps..." % base)
        probe = probe_site(
            fetch_text, base,
            max_sitemaps=self.config.max_sitemaps,
            max_entries=self.config.max_sitemap_entries,
            logger=self._log,
        )
        self.probe = probe

        urls = probe.urls(limit=self.config.max_pages if self.config.max_pages > 0 else None)
        estimate = estimate_from_links(urls)
        recommended = recommend_presets(estimate)

        return {
            "probe": probe.as_dict(),
            "sample_urls": urls[:25],
            "estimate": estimate,
            "recommended": recommended,
            "notes": list(probe.notes),
        }

    def run(self) -> Dict:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        seeds = self._prepare_seeds()
        if not seeds:
            raise ValueError("no valid seed URLs (need http/https URLs)")

        self._log("seeds: %s" % ", ".join(u for u, _ in seeds))
        self._log("scope: max_depth=%s max_pages=%s concurrency=%d delay=%.2fs robots=%s sitemap=%s"
                  % ("unlimited" if self.config.unlimited_depth else self.config.max_depth,
                     "unlimited" if self.config.unlimited else self.config.max_pages,
                     self.config.concurrency, self.config.delay,
                     "on" if self.config.respect_robots else "off",
                     "on" if self.config.use_sitemap else "off"))

        resumed = self._resume_if_possible()
        if not resumed:
            self._state.signature = self._signature()

        with Fetcher(self.config, logger=self.logger) as fetcher:
            self._authenticate(fetcher)
            if self.config.use_sitemap:
                self._seed_from_sitemap(fetcher, seeds)

            while self._state.pending and not self._budget_exhausted():
                batch = self._take_batch()
                if not batch:
                    break
                batch_depth = batch[0][1]

                self._log("depth %d: fetching %d url(s), %d queued"
                          % (batch_depth, len(batch), len(self._state.pending)))

                for record, children in self._fetch_batch(fetcher, batch):
                    self._pages.append(record)
                    self._state.pages_crawled += 1
                    for child in children:
                        if child in self._seen:
                            continue
                        self._state.push(child, batch_depth + 1)
                        self._log("  + %s (depth %d)" % (child, batch_depth + 1))

                if self.config.state_file:
                    save_state(self.config.state_file, self._state)

                if self._should_stop_for_errors():
                    self._log("stopping: too many consecutive failures")
                    break

        elapsed = time.monotonic() - started
        result = self._finalize(elapsed=elapsed, started_at=started_at, seeds=seeds,
                                resumed=resumed)
        if self.config.state_file:
            # Saved again so the coverage set written during finalize persists.
            save_state(self.config.state_file, self._state)
        if self.config.decisions_file:
            self.interactor.save(self.config.decisions_file)
        return result

    def _authenticate(self, fetcher: Fetcher) -> None:
        """Sign in once, before any content request, if configured to."""
        if not self.config.has_login:
            return
        result = fetcher.authenticate(prompt_code=self._prompt_2fa)
        self._auth_result = result
        self._log("auth: %s (%s)" % ("ok" if result.ok else "failed", result.method))

    def _prompt_2fa(self, message: str) -> str:
        """Ask for a 2FA code through the interactor, respecting ask/auto/never."""
        answer = self.interactor.get("totp_code", message.strip(), "", "")
        return str(answer.value or "")

    def _resume_if_possible(self) -> bool:
        if not self.config.state_file:
            return False
        loaded = load_state(self.config.state_file)
        if loaded is None:
            return False
        if loaded.signature and loaded.signature != self._signature():
            self._log("state file targets a different site, starting fresh")
            return False
        self._state = loaded
        self._state.signature = self._signature()
        self._seen = set(loaded.seen)
        self._log("resuming: %d url(s) already seen, %d pending, %d page(s) done"
                  % (len(loaded.seen), len(loaded.pending), loaded.pages_crawled))
        return True

    def _signature(self) -> str:
        """Identity of this crawl target, used to validate a resumed state file."""
        hosts = sorted({u for u in map(self._seed_url, self.config.seeds) if u})
        return "|".join(hosts)

    def _seed_from_sitemap(self, fetcher: Fetcher, seeds) -> None:
        base = self._seed_url(seeds[0][0]) if seeds else None
        if not base:
            return

        def fetch_text(url: str) -> Optional[str]:
            result = fetcher.get(url)
            return result.text if result.ok else None

        probe = probe_site(
            fetch_text, base,
            max_sitemaps=self.config.max_sitemaps,
            max_entries=self.config.max_sitemap_entries,
            logger=self._log,
        )
        self.probe = probe
        urls = probe.urls(
            limit=self.config.max_pages if self.config.max_pages > 0 else None
        )
        self.total_known = len(urls)
        self._sitemap_urls = set(urls)
        self._log("sitemap: %d url(s) from %d map(s)"
                  % (len(urls), len(probe.sitemap_sources)))

        added = 0
        for url in urls:
            if self._in_scope(url):
                self._state.push(url, 1)
                added += 1
        if added:
            self._log("queued %d sitemap url(s) at depth 1" % added)

    def _budget_exhausted(self) -> bool:
        if self.config.unlimited:
            return False
        return len(self._pages) >= self.config.max_pages

    def _take_batch(self) -> List[Tuple[str, int]]:
        remaining = -1
        if not self.config.unlimited:
            remaining = max(self.config.max_pages - len(self._pages), 0)
        size = self.config.concurrency if remaining < 0 \
            else min(self.config.concurrency, remaining)
        if size <= 0:
            return []
        max_depth = -1 if self.config.unlimited_depth else self.config.max_depth
        return self._state.pop_batch(size, max_depth)

    def _should_stop_for_errors(self) -> bool:
        if self.config.stop_on_error_ratio <= 0 or self.config.stop_on_error_ratio > 1:
            return False
        recent = self._pages[-50:]
        if len(recent) < 20:
            return False
        failures = sum(1 for p in recent if p.get("error"))
        return (failures / float(len(recent))) > self.config.stop_on_error_ratio

    @staticmethod
    def _seed_url(raw: str) -> Optional[str]:
        candidate = (raw or "").strip()
        if not candidate:
            return None
        scheme = candidate.split(":", 1)[0].lower() if ":" in candidate else ""
        if scheme and not scheme.replace("+", "").replace(".", "").replace("-", "").isalnum():
            return None
        if scheme in ("http", "https"):
            return normalize_url(candidate)
        if scheme:
            return None
        return normalize_url("http://" + candidate)

    def _prepare_seeds(self) -> List[Tuple[str, int]]:
        prepared = []
        for raw in self.config.seeds:
            url = self._seed_url(raw)
            if url is None:
                self._log("skipping invalid seed: %r" % raw)
                continue
            if not self._in_scope(url, is_seed=True):
                self._log("seed out of scope: %s" % url)
                continue
            with self._lock:
                if url in self._seen:
                    continue
                self._seen.add(url)
            prepared.append((url, 0))
        for url, depth in prepared:
            self._state.push(url, depth)
        return prepared

    def _in_scope(self, url: str, is_seed: bool = False) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if parts.path.startswith(("/wp-admin", "/wp-login", "/admin", "/login")):
            return False

        if self.config.allowed_domains:
            allowed = {d.lower().lstrip(".") for d in self.config.allowed_domains}
            if not self._host_allowed(host, allowed):
                return False
        elif not self.config.follow_external and not self._host_matches_seeds(host):
            return False

        if self.config.blocked_domains:
            blocked = {d.lower().lstrip(".") for d in self.config.blocked_domains}
            if self._host_allowed(host, blocked):
                return False

        if not is_seed:
            if self.config.include_patterns:
                if not any(re.search(p, url) for p in self.config.include_patterns):
                    return False
            if self.config.exclude_patterns:
                if any(re.search(p, url) for p in self.config.exclude_patterns):
                    return False
        return True

    @staticmethod
    def _host_allowed(host: str, domains: Set[str]) -> bool:
        if host in domains or registrable_domain(host) in domains:
            return True
        return any(host.endswith("." + d) for d in domains)

    def _host_matches_seeds(self, host: str) -> bool:
        for raw in self.config.seeds or []:
            url = self._seed_url(raw)
            if not url:
                continue
            seed_host = (urlsplit(url).hostname or "").lower()
            if not seed_host:
                continue
            if host == seed_host or registrable_domain(host) == registrable_domain(seed_host):
                return True
        return False

    def _fetch_batch(self, fetcher: Fetcher, batch: List[Tuple[str, int]]
                 ) -> List[Tuple[Dict, List[str]]]:
        """Fetch a batch, dropping any URL blocked by robots.txt."""
        results: List[Tuple[Dict, List[str]]] = []
        workers = max(1, min(self.config.concurrency, len(batch)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._fetch_one, fetcher, url, depth): url
                       for url, depth in batch}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # keep one bad URL from killing the crawl
                    self._counts["errors"] += 1
                    self._log("  ! %s failed: %s: %s"
                              % (url, type(exc).__name__, exc))
                    outcome = (self._error_record(url, depth, exc), [])
                if outcome is not None:
                    results.append(outcome)
        return results

    def _fetch_one(self, fetcher: Fetcher, url: str, depth: int
                   ) -> Optional[Tuple[Dict, List[str]]]:
        result = fetcher.get(url)

        if result.error == "blocked-by-robots":
            self._counts["robots_blocked"] += 1
            self._log("  - %s blocked by robots.txt" % url)
            return None

        if result.error and not result.status:
            self._counts["errors"] += 1
            self._log("  ! %s %s" % (url, result.error))
            return self._error_record(url, depth, result.error), []

        if not result.ok:
            self._counts["errors"] += 1
            self._log("  ! %s HTTP %d" % (url, result.status))
            record = self._base_record(url, depth, result)
            record["error"] = "http-%d" % result.status
            record["internal_links"] = []
            record["external_links"] = []
            record["internal_links_count"] = 0
            record["external_links_count"] = 0
            record["emails"] = []
            record["host"] = (urlsplit(url).hostname or "")
            return record, []

        record = self._base_record(url, depth, result)
        record["host"] = (urlsplit(url).hostname or "")

        if not result.is_html:
            self._log("  + %s (skipped non-HTML %s)" % (url, result.content_type or "?"))
            self._counts["skipped"] += 1
            record["skipped"] = True
            return record, []

        html_text = result.text
        record.update(extract_page(html_text, url))
        record["emails"] = extract_emails(html_text)
        record["top_keywords"] = [
            {"term": term, "count": count} for term, count in word_frequencies(html_text, 10)
        ]

        if self.config.dedupe_content and self._is_duplicate(record):
            self._counts["duplicates"] += 1
            record["duplicate"] = True
            self._log("  = %s (duplicate content)" % url)
            return record, []

        links = extract_links(html_text, result.url, self.config.follow_nofollow)
        record["internal_links"] = links["internal"]
        record["external_links"] = links["external"]
        record["internal_links_count"] = len(links["internal"])
        record["external_links_count"] = len(links["external"])
        record["nofollow_links_count"] = len(links["nofollow"])

        children = links["internal"] if not self.config.follow_external \
            else links["internal"] + links["external"]
        children = self._filter_children(children)

        signals = scan_page(
            html_text, result.url,
            word_count=record.get("word_count", 0),
            link_count=record.get("internal_links_count", 0),
        )
        record["findings"] = [f.as_dict() for f in signals.findings()]
        record["needs_login"] = signals.needs_login
        if signals.needs_login:
            children = []

        extra = self.agent.consider(result.url, signals, depth)
        if extra:
            known = set(children)
            children.extend(u for u in extra if u not in known)

        if looks_js_rendered(html_text, record.get("internal_links_count", 0),
                             record.get("word_count", 0)):
            record["js_rendered"] = True
            self._js_heavy_pages += 1

        if result.truncated:
            record["truncated"] = True
        if self.config.save_html:
            record["saved_html"] = save_html(
                os.path.join(self.config.output_dir, "pages"), result.url, html_text
            )

        self._log("  + %s (%d words, %d in / %d out)"
                  % (url, record.get("word_count", 0),
                     len(links["internal"]), len(links["external"])))
        return record, children

    def _is_duplicate(self, record: Dict) -> bool:
        preview = (record.get("text_preview") or "").strip()
        if len(preview) < 200:
            return False
        digest = hashlib.sha1(preview.encode("utf-8", "replace")).hexdigest()
        with self._lock:
            first = self._content_hashes.setdefault(digest, record["url"])
        return first != record["url"]

    def _filter_children(self, children: List[str]) -> List[str]:
        kept = []
        for child in children:
            if self.config.strip_params:
                cleaned = normalize_url(strip_tracking_params(child))
                if cleaned:
                    child = cleaned
            if child in self._seen or child in self._state.seen:
                continue
            if not is_crawlable_url(child):
                self._counts["skipped"] += 1
                continue
            if not self._in_scope(child):
                continue
            kept.append(child)
        return kept

    def _base_record(self, url: str, depth: int, result) -> Dict:
        return {
            "url": url,
            "depth": depth,
            "status": result.status,
            "content_type": result.content_type,
            "fetch_ms": round(result.elapsed * 1000.0, 1),
            "truncated": getattr(result, "truncated", False),
            "error": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _error_record(self, url: str, depth: int, error) -> Dict:
        return {
            "url": url,
            "depth": depth,
            "status": 0,
            "content_type": "",
            "title": "",
            "meta_description": "",
            "word_count": 0,
            "internal_links": [],
            "external_links": [],
            "internal_links_count": 0,
            "external_links_count": 0,
            "emails": [],
            "fetch_ms": 0.0,
            "error": str(error),
            "host": (urlsplit(url).hostname or ""),
        }

    def _finalize(self, elapsed: float, started_at: str, seeds, resumed: bool = False) -> Dict:
        pages = self._pages
        self._pages = []
        successful = [p for p in pages if not p.get("error")]
        all_emails = sorted({e for p in pages for e in (p.get("emails") or [])})

        coverage = None
        if self.total_known:
            # Coverage is measured against the sitemap, not against every page we
            # happened to reach: link discovery can surface pages the sitemap
            # never listed, and those must not dilute the real figure. The set of
            # covered sitemap URLs is cumulative across resumed runs.
            for page in pages:
                if not page.get("error") and page.get("url") in self._sitemap_urls:
                    self._state.covered.add(page["url"])
            sitemap_known = len(self._sitemap_urls)
            done = len(self._state.covered)
            coverage = {
                "known_urls": sitemap_known,
                "crawled": done,
                "percent": round(100.0 * done / sitemap_known, 1) if sitemap_known else 0.0,
                "remaining": max(sitemap_known - done, 0),
                "off_sitemap_pages": max(len(successful) - done, 0),
            }

        summary = {
            "started_at": started_at,
            "elapsed": round(elapsed, 2),
            "seeds": [u for u, _ in seeds],
            "resumed": resumed,
            "pages_crawled": len(pages),
            "successful": len(successful),
            "failed": len(pages) - len(successful),
            "skipped": self._counts["skipped"],
            "robots_blocked": self._counts["robots_blocked"],
            "duplicates": self._counts["duplicates"],
            "js_rendered_pages": self._js_heavy_pages,
            "total_words": sum(p.get("word_count", 0) or 0 for p in pages),
            "total_bytes": sum(len(p.get("text_preview", "") or "") for p in pages),
            "unique_hosts": len({p.get("host", "") for p in pages if p.get("host")}),
            "all_emails": all_emails,
            "pending": len(self._state.pending),
            "coverage": coverage,
            "agent": self.agent.summary(),
            "auth": self._auth_summary(),
            "login_walls": sorted({p["url"] for p in pages if p.get("needs_login")}),
            "site": self.probe.as_dict() if self.probe else None,
            "config": {
                "max_depth": self.config.max_depth,
                "max_pages": self.config.max_pages,
                "unlimited": self.config.unlimited,
                "concurrency": self.config.concurrency,
                "delay": self.config.delay,
                "respect_robots": self.config.respect_robots,
                "follow_external": self.config.follow_external,
                "use_sitemap": self.config.use_sitemap,
                "dedupe_content": self.config.dedupe_content,
                "user_agent": self.config.user_agent,
            },
        }
        return {"summary": summary, "pages": pages}

    def _auth_summary(self) -> Dict:
        """Login outcome, with no credentials in it."""
        info = describe_auth(self.config)
        if self._auth_result is not None:
            info["attempted"] = True
            info["ok"] = self._auth_result.ok
            info["method"] = self._auth_result.method
            info["reason"] = self._auth_result.reason
            info["needs_totp"] = self._auth_result.needs_totp
            info["needs_captcha"] = self._auth_result.needs_captcha
        else:
            info["attempted"] = False
        return info


def crawl(config: CrawlConfig, logger: Logger = None, output_dir: Optional[str] = None) -> Dict:
    crawler = Crawler(config, logger=logger)
    result = crawler.run()
    if output_dir:
        from .report import write_reports
        result["reports"] = write_reports(
            result["pages"], result["summary"], output_dir=output_dir
        )
    return result


def save_html(directory: str, url: str, html_text: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, digest + ".html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    return path