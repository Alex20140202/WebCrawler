from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from .config import CrawlConfig
from .fetcher import Fetcher
from .parser import (
    extract_emails,
    extract_links,
    extract_page,
    normalize_url,
    registrable_domain,
    word_frequencies,
)

Logger = Optional[Callable[[str], None]]


class Crawler:
    """Breadth-first, rate-limited, robots-aware web crawler."""

    def __init__(self, config: CrawlConfig, logger: Logger = None):
        self.config = config
        self.logger = logger
        self._seen = set()
        self._lock = threading.Lock()
        self._pages: List[Dict] = []
        self._counts = {"skipped": 0, "robots_blocked": 0, "errors": 0}

    def _log(self, message: str) -> None:
        if self.logger and self.config.verbose:
            self.logger(message)

    def run(self) -> Dict:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        seeds = self._prepare_seeds()
        if not seeds:
            raise ValueError("no valid seed URLs (need http/https URLs)")

        self._log("seeds: %s" % ", ".join(u for u, _ in seeds))
        self._log("max_depth=%d max_pages=%d concurrency=%d delay=%.2fs robots=%s"
                  % (self.config.max_depth, self.config.max_pages,
                     self.config.concurrency, self.config.delay,
                     "on" if self.config.respect_robots else "off"))

        with Fetcher(self.config) as fetcher:
            frontier = deque(seeds)
            while frontier and len(self._pages) < self.config.max_pages:
                batch = self._take_batch(frontier)
                if not batch:
                    break
                batch_depth = batch[0][1]

                self._log("depth %d: fetching %d url(s), %d queued"
                          % (batch_depth, len(batch), len(frontier)))

                for record, children in self._fetch_batch(fetcher, batch):
                    self._pages.append(record)
                    for child in children:
                        with self._lock:
                            if child in self._seen:
                                continue
                            self._seen.add(child)
                        frontier.append((child, batch_depth + 1))
                        self._log("  + %s (depth %d)" % (child, batch_depth + 1))

        elapsed = time.monotonic() - started
        return self._finalize(elapsed=elapsed, started_at=started_at, seeds=seeds)

    def _take_batch(self, frontier: "deque") -> List[Tuple[str, int]]:
        remaining = self.config.max_pages - len(self._pages)
        size = min(self.config.concurrency, max(remaining, 0))
        if size <= 0:
            return []
        batch = []
        while frontier and len(batch) < size:
            url, depth = frontier.popleft()
            if depth > self.config.max_depth:
                self._counts["skipped"] += 1
                continue
            batch.append((url, depth))
        return batch

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
        results: List[Tuple[Dict, List[str]]] = []
        workers = max(1, min(self.config.concurrency, len(batch)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._fetch_one, fetcher, url, depth): url
                       for url, depth in batch}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # keep one bad URL from killing the crawl
                    self._counts["errors"] += 1
                    self._log("  ! %s failed: %s: %s"
                              % (url, type(exc).__name__, exc))
                    results.append((self._error_record(url, depth, exc), []))
        return results

    def _fetch_one(self, fetcher: Fetcher, url: str, depth: int) -> Tuple[Dict, List[str]]:
        result = fetcher.get(url)

        if result.error == "blocked-by-robots":
            self._counts["robots_blocked"] += 1
            self._log("  - %s blocked by robots.txt" % url)
            return self._error_record(url, depth, RuntimeError("blocked-by-robots")), []

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

        links = extract_links(html_text, result.url, self.config.follow_nofollow)
        record["internal_links"] = links["internal"]
        record["external_links"] = links["external"]
        record["internal_links_count"] = len(links["internal"])
        record["external_links_count"] = len(links["external"])
        record["nofollow_links_count"] = len(links["nofollow"])

        children = links["internal"] if not self.config.follow_external \
            else links["internal"] + links["external"]
        children = [c for c in children if c not in self._seen and self._in_scope(c)]
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

    def _finalize(self, elapsed: float, started_at: str, seeds) -> Dict:
        pages = self._pages
        self._pages = []
        successful = [p for p in pages if not p.get("error")]
        all_emails = sorted({e for p in pages for e in (p.get("emails") or [])})

        summary = {
            "started_at": started_at,
            "elapsed": round(elapsed, 2),
            "seeds": [u for u, _ in seeds],
            "pages_crawled": len(pages),
            "successful": len(successful),
            "failed": len(pages) - len(successful),
            "skipped": self._counts["skipped"],
            "robots_blocked": self._counts["robots_blocked"],
            "total_words": sum(p.get("word_count", 0) or 0 for p in pages),
            "total_bytes": sum(len(p.get("text_preview", "") or "") for p in pages),
            "unique_hosts": len({p.get("host", "") for p in pages if p.get("host")}),
            "all_emails": all_emails,
            "config": {
                "max_depth": self.config.max_depth,
                "max_pages": self.config.max_pages,
                "concurrency": self.config.concurrency,
                "delay": self.config.delay,
                "respect_robots": self.config.respect_robots,
                "follow_external": self.config.follow_external,
                "user_agent": self.config.user_agent,
            },
        }
        return {"summary": summary, "pages": pages}


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