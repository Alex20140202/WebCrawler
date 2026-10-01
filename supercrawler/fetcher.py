from __future__ import annotations

import random
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from .config import CrawlConfig

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
HTML_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}
)


class FetchResult:
    __slots__ = ("url", "status", "text", "content_type", "elapsed", "truncated", "error")

    def __init__(self, url, status=0, text="", content_type="", elapsed=0.0,
                 truncated=False, error=None):
        self.url = url
        self.status = status
        self.text = text
        self.content_type = content_type
        self.elapsed = elapsed
        self.truncated = truncated
        self.error = error

    @property
    def ok(self):
        return self.error is None and 200 <= self.status < 300

    @property
    def is_html(self):
        return self.content_type in HTML_TYPES


class Throttle:
    def __init__(self, per_host: int, delay: float):
        self.per_host = max(1, per_host)
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._next_free = {}
        self._active = {}

    def acquire(self, host: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                active = self._active.get(host, 0)
                ready_at = self._next_free.get(host, 0.0)
                if active < self.per_host and now >= ready_at:
                    self._active[host] = active + 1
                    return
                wait = max(ready_at - now, 0.02)
            time.sleep(wait)

    def release(self, host: str) -> None:
        with self._lock:
            remaining = self._active.get(host, 1) - 1
            self._active[host] = max(0, remaining)
            self._next_free[host] = time.monotonic() + self.delay


class Fetcher:
    def __init__(self, config: CrawlConfig):
        self.config = config
        self.throttle = Throttle(config.per_host_concurrency, config.delay)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        })
        pool = max(16, config.concurrency * 2)
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._robots = {}
        self._robots_lock = threading.Lock()

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def allowed_by_robots(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.config.user_agent, url)

    def crawl_delay(self, url: str) -> float:
        if not self.config.respect_robots:
            return self.config.delay
        parser = self._robots_for(url)
        if parser is None:
            return self.config.delay
        try:
            value = parser.crawl_delay(self.config.user_agent)
        except Exception:
            value = None
        return max(self.config.delay, value or 0.0)

    def _robots_for(self, url: str):
        parts = urlsplit(url)
        root = "%s://%s" % (parts.scheme, parts.netloc)
        with self._robots_lock:
            if root in self._robots:
                return self._robots[root]

        parser = None
        response = self._request(root + "/robots.txt")
        if response is not None and response.ok and response.text.strip():
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())

        with self._robots_lock:
            if root not in self._robots:
                self._robots[root] = parser
            return self._robots[root]

    def get(self, url: str) -> FetchResult:
        if self.config.respect_robots and not self.allowed_by_robots(url):
            return FetchResult(url, error="blocked-by-robots")
        attempts = max(1, self.config.max_retries + 1)
        for attempt in range(attempts):
            result = self._request(url)
            if result is None:
                if attempt + 1 < attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                return FetchResult(url, error="request-failed")
            if result.error == "too-large" or not result.error:
                return result
            if result.status in RETRY_STATUS and attempt + 1 < attempts:
                time.sleep(self._backoff(attempt))
                continue
            return result
        return FetchResult(url, error="request-failed")

    def _backoff(self, attempt: int) -> float:
        base = self.config.backoff_factor * (2 ** attempt)
        return base + random.random() * 0.25

    def _request(self, url: str):
        host = urlsplit(url).hostname or ""
        started = time.monotonic()
        self.throttle.acquire(host)
        try:
            response = self.session.get(
                url,
                timeout=self.config.timeout,
                allow_redirects=True,
                stream=True,
            )
        except requests.RequestException as exc:
            return FetchResult(url, error="request-failed: %s" % type(exc).__name__)
        finally:
            self.throttle.release(host)

        raw = bytearray()
        truncated = False
        try:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                room = self.config.max_bytes - len(raw)
                if room <= 0:
                    truncated = True
                    break
                raw.extend(chunk[:room])
                if len(raw) >= self.config.max_bytes:
                    truncated = True
                    break
        except requests.RequestException as exc:
            response.close()
            return FetchResult(url, error="read-failed: %s" % type(exc).__name__)

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        encoding = response.encoding or "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")

        elapsed = time.monotonic() - started
        return FetchResult(
            url=url,
            status=response.status_code,
            text=text,
            content_type=content_type,
            elapsed=elapsed,
            truncated=truncated,
        )