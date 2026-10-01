from __future__ import annotations

import random
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from .auth import AuthError, Authenticator, LoginResult
from .config import CrawlConfig

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
HTML_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}
)

# A fetched page with almost no visible text is usually a shell that needs
# JavaScript. The bar is measured on text with tags removed: counting tokens in
# raw markup would count every attribute as content and never fire.
_MIN_VISIBLE_CHARS = 400
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")

# Markers of a page that already has real content.
_CONTENT_MARKERS = (
    "<article", "</p>", "</li>", "entry-content", "post-content", "class=\"content",
)


def visible_text_length(html: str) -> int:
    """Rough count of characters a user would actually see."""
    if not html:
        return 0
    cleaned = _SCRIPT_RE.sub(" ", html)
    cleaned = _TAG_RE.sub(" ", cleaned)
    return len(re.sub(r"\s+", " ", cleaned).strip())


def _needs_render(result) -> bool:
    if result.error or not result.ok or not result.is_html:
        return False
    lowered = result.text.lower()
    if any(marker in lowered for marker in _CONTENT_MARKERS):
        return False
    return visible_text_length(result.text) < _MIN_VISIBLE_CHARS


def _split_auth_header(raw: str):
    if ":" in raw:
        name, value = raw.split(":", 1)
        return name.strip(), value.strip()
    if "=" in raw:
        name, value = raw.split("=", 1)
        return name.strip(), value.strip()
    return "", ""


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
    def __init__(self, config: CrawlConfig, logger=None, renderer=None):
        self.config = config
        self.logger = logger
        self.renderer = renderer
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
        self.auth_result = None
        self.auth_info = {}

    def _log(self, message: str) -> None:
        if self.logger and self.config.verbose:
            self.logger(message)

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def authenticate(self, prompt_code=None) -> LoginResult:
        """Sign in before crawling. Raises AuthError when a login is required."""
        authenticator = Authenticator(self.session, self.config,
                                      logger=self._log, prompt_code=prompt_code,
                                      renderer=self.renderer)

        reusable = bool(self.config.session_file or self.config.auth_header)
        if not authenticator.enabled:
            if not reusable:
                return LoginResult(True, "none", "no login configured")
            # No credentials to post, but a saved session or auth header still
            # needs applying before the first content request.
            self.auth_info = authenticator.prepare()
            self.auth_result = LoginResult(True, "session",
                                           "using saved session or auth header")
            return self.auth_result

        self.auth_info = authenticator.prepare()

        if self.config.login_url:
            # The login page is a credential POST, not a crawl of public
            # content, so the robots check for content URLs does not apply.
            self._log("authenticating at %s" % self.config.login_url)
            result = authenticator.login()
            self.auth_result = result
            if not result.ok and self.config.require_login:
                raise AuthError(result.reason or "login failed")
            if not result.ok:
                self._log("continuing without a session; protected pages will 403")
            return result

        self.auth_result = LoginResult(True, "session", "using saved session")
        return self.auth_result

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

    def get(self, url: str, allow_render: bool = True) -> FetchResult:
        if self.config.respect_robots and not self.allowed_by_robots(url):
            return FetchResult(url, error="blocked-by-robots")

        result = self._get_with_retries(url)
        if (allow_render and self.renderer is not None
                and _needs_render(result)):
            return self._render(url, result)
        return result

    def _get_with_retries(self, url: str) -> FetchResult:
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

    def _render(self, url: str, shell: FetchResult) -> FetchResult:
        """A page that came back as an empty shell gets loaded in a browser."""
        self._log("  rendering %s (server HTML had no content)" % url)
        extra = {}
        if self.config.auth_header:
            name, value = _split_auth_header(self.config.auth_header)
            if name and value:
                extra[name] = value

        rendered = self.renderer.render(url, session=self.session,
                                        extra_headers=extra or None)
        if not rendered.ok:
            self._log("  ! render failed: %s" % (rendered.error or "unknown"))
            rendered.error = None
            return shell

        elapsed = shell.elapsed + rendered.elapsed
        return FetchResult(
            url=rendered.url,
            status=rendered.status or shell.status,
            text=rendered.html,
            content_type="text/html",
            elapsed=elapsed,
            error=None,
        )

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