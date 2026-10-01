from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Sequence

Logger = Optional[Callable[[str], None]]

DEFAULT_WAIT_UNTIL = "networkidle"
DEFAULT_SETTLE_MS = 400
DEFAULT_TIMEOUT_MS = 30000


class RenderError(Exception):
    """Rendering could not run. Never carries credentials."""


class RenderResult:
    __slots__ = ("url", "html", "status", "elapsed", "error", "title", "from_cache")

    def __init__(self, url, html="", status=0, elapsed=0.0, error=None,
                 title="", from_cache=False):
        self.url = url
        self.html = html
        self.status = status
        self.elapsed = elapsed
        self.error = error
        self.title = title
        self.from_cache = from_cache

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.html)

    def as_dict(self) -> Dict:
        return {"url": self.url, "status": self.status,
                "elapsed": round(self.elapsed, 2), "error": self.error,
                "title": self.title, "from_cache": self.from_cache,
                "bytes": len(self.html)}


def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False


def playwright_hint() -> str:
    return (
        "rendering needs playwright:\n"
        "  python3 -m pip install playwright\n"
        "  python3 -m playwright install chromium"
    )


class RenderCache:
    """Rendered HTML on disk, so repeat runs and resumes skip the browser."""

    def __init__(self, directory: str):
        self.directory = directory
        self.index_path = os.path.join(directory, "index.json")
        self._index: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.index_path):
            return
        try:
            with open(self.index_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(payload, dict):
            self._index = payload.get("entries", {}) or {}

    def _save(self) -> None:
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = self.index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"entries": self._index}, handle, ensure_ascii=False)
            os.replace(tmp, self.index_path)
        except OSError:
            pass

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def path_for(self, url: str) -> str:
        return os.path.join(self.directory, self.key(url) + ".html")

    def get(self, url: str) -> Optional[RenderResult]:
        with self._lock:
            entry = self._index.get(url)
        if not entry:
            return None
        path = self.path_for(url)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                html = handle.read()
        except OSError:
            return None
        return RenderResult(url, html=html, status=entry.get("status", 200),
                            title=entry.get("title", ""), from_cache=True)

    def put(self, result: RenderResult) -> None:
        if not result.ok:
            return
        with self._lock:
            os.makedirs(self.directory, exist_ok=True)
            try:
                with open(self.path_for(result.url), "w", encoding="utf-8") as handle:
                    handle.write(result.html)
            except OSError:
                return
            self._index[result.url] = {
                "status": result.status,
                "title": result.title,
                "rendered_at": time.time(),
            }
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._index = {}
            try:
                if os.path.isdir(self.directory):
                    for name in os.listdir(self.directory):
                        os.remove(os.path.join(self.directory, name))
            except OSError:
                pass

    def __len__(self) -> int:
        return len(self._index)


class Renderer:
    """Loads pages in a real browser so JavaScript-rendered sites can be read.

    Playwright's synchronous API is bound to the thread that created it, so the
    browser lives on a private single-worker executor and every call is queued
    onto it. HTML parsing after rendering still runs in the caller's own pool.
    """

    def __init__(self, config, cache: Optional[RenderCache] = None,
                 logger: Logger = None):
        self.config = config
        self.cache = cache
        self.logger = logger
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._started = False
        self.rendered = 0
        self.cache_hits = 0
        self.failures = 0

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "render", False))

    def _start(self, cookies: Optional[Sequence[Dict]] = None,
               extra_headers: Optional[Dict[str, str]] = None) -> None:
        if self._started:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            raise RenderError("playwright is not installed\n" + playwright_hint())

        try:
            self._playwright = sync_playwright().start()
        except Exception as exc:
            raise RenderError("could not start playwright: %s\n%s"
                              % (type(exc).__name__, playwright_hint()))

        try:
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
        except Exception as exc:
            self._close()
            raise RenderError(
                "could not launch chromium: %s\n%s" % (type(exc).__name__,
                                                       playwright_hint()))

        context_args: Dict = {
            "user_agent": getattr(self.config, "user_agent", None) or
            "SuperCrawler/1.0 (rendering)",
            "ignore_https_errors": True,
        }
        if extra_headers:
            context_args["extra_http_headers"] = extra_headers
        self._context = self._browser.new_context(**context_args)

        if cookies:
            try:
                self._context.add_cookies([
                    {"name": c.get("name"), "value": c.get("value"),
                     "domain": c.get("domain"), "path": c.get("path", "/")}
                    for c in cookies if c.get("name") and c.get("value")
                ])
                self._log("renderer: injected %d cookie(s)" % len(cookies))
            except Exception as exc:
                self._log("renderer: could not inject cookies (%s)"
                          % type(exc).__name__)

        self._started = True

    def _close(self) -> None:
        for obj, name in ((self._context, "context"), (self._browser, "browser")):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
            setattr(self, "_" + name, None)
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._started = False

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="supercrawler-render")
            return self._executor

    def close(self) -> None:
        executor = self._executor
        if executor is not None:
            try:
                executor.submit(self._close).result(timeout=30)
            except Exception:
                pass
            executor.shutdown(wait=False)
            self._executor = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def render(self, url: str, session=None, extra_headers: Optional[Dict] = None,
               use_cache: bool = True) -> RenderResult:
        """Fetch one URL through the browser.

        The fragment is preserved: on a hash-routed app '#/blog' is the only
        thing that differs from '/', and dropping it would return the same
        shell for every route.
        """
        if use_cache and self.cache is not None:
            cached = self.cache.get(url)
            if cached is not None:
                self.cache_hits += 1
                return cached

        cookies = _cookies_from_session(session)
        executor = self._ensure_executor()
        try:
            result = executor.submit(
                self._render_on_worker, url, cookies, extra_headers
            ).result()
        except Exception as exc:
            self.failures += 1
            return RenderResult(url, error="render-failed: %s" % _brief(exc))

        if result.ok:
            self.rendered += 1
            if self.cache is not None:
                self.cache.put(result)
        else:
            self.failures += 1
        return result

    def _render_on_worker(self, url: str, cookies, extra_headers) -> RenderResult:
        """Runs only on the renderer's own thread, where the browser lives."""
        started = time.monotonic()
        try:
            self._start(cookies=cookies, extra_headers=extra_headers)
        except RenderError as exc:
            return RenderResult(url, error=str(exc))

        page = None
        try:
            page = self._context.new_page()
            response = self._goto(page, url)
            settle = int(getattr(self.config, "render_settle_ms",
                                 DEFAULT_SETTLE_MS))
            if settle > 0:
                page.wait_for_timeout(settle)
            html = page.content()
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            status = response.status if response is not None else 200
            return RenderResult(url, html=html, status=status,
                                elapsed=time.monotonic() - started, title=title)
        except Exception as exc:
            return RenderResult(url, elapsed=time.monotonic() - started,
                                error="render-failed: %s" % _brief(exc))
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass

    def _goto(self, page, url: str):
        """Load a URL, relaxing the readiness signal if it never arrives.

        A single-page app can hold a socket open or poll forever, so
        'networkidle' may simply never fire. Falling back to
        'domcontentloaded' plus the settle delay is far more reliable.
        """
        timeout = int(getattr(self.config, "render_timeout", DEFAULT_TIMEOUT_MS))
        wanted = getattr(self.config, "render_wait_until", DEFAULT_WAIT_UNTIL)
        order = [wanted]
        if wanted == "networkidle":
            order.append("load")
        order.append("domcontentloaded")

        last = None
        for index, wait_until in enumerate(order):
            try:
                return page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as exc:
                last = exc
                if index + 1 < len(order) and _is_timeout(exc):
                    self._log("renderer: %s did not settle, retrying with %s"
                              % (wait_until, order[index + 1]))
                    # The navigation may already have landed despite the wait.
                    try:
                        if page.url and page.url != "about:blank":
                            return None
                    except Exception:
                        pass
        raise last if last else RuntimeError("navigation failed")


    def _login_on_worker(self, url: str, username: str, password: str,
                         code: Optional[str]) -> LoginOutcome:
        """Fill and submit a login form in the page. Runs on the render thread."""
        try:
            self._start()
        except RenderError as exc:
            return LoginOutcome(False, str(exc))

        timeout = int(getattr(self.config, "render_timeout", DEFAULT_TIMEOUT_MS))
        page = None
        try:
            page = self._context.new_page()
            self._goto(page, url)
            self._settle(page)

            if _first_present(page, TOTP_SELECTORS) and code is None:
                return LoginOutcome(False, "second factor required", [],
                                    needs_totp=True, url=page.url)

            user_selector = _first_present(page, USER_SELECTORS)
            pass_selector = _first_present(page, PASS_SELECTORS)
            if not pass_selector:
                return LoginOutcome(False, "no password field on the login page",
                                    url=page.url)

            self._log("browser login: filling form on %s" % url)
            if user_selector:
                page.fill(user_selector, username)
            page.fill(pass_selector, password)

            submit = _first_present(page, SUBMIT_SELECTORS)
            if submit:
                page.click(submit)
            else:
                page.press(pass_selector, "Enter")

            # Wait for the app to react: either the password field goes away or
            # we leave the login route.
            self._wait_for_login(page, timeout)

            if _first_present(page, PASS_SELECTORS) is not None:
                cookies = self._context.cookies()
                return LoginOutcome(False, "still on the login form after "
                                    "submitting", cookies, url=page.url)

            cookies = self._context.cookies()
            self._log("browser login: succeeded, %d cookie(s)" % len(cookies))
            return LoginOutcome(True, "", cookies, url=page.url)
        except Exception as exc:
            return LoginOutcome(False, "login-failed: %s" % _brief(exc))
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass

    def _settle(self, page) -> None:
        settle = int(getattr(self.config, "render_settle_ms", DEFAULT_SETTLE_MS))
        if settle > 0:
            page.wait_for_timeout(settle)

    def _wait_for_login(self, page, timeout_ms: int) -> None:
        """Poll briefly for the login to take effect."""
        deadline_ms = min(timeout_ms, 15000)
        step = 250
        waited = 0
        while waited < deadline_ms:
            try:
                if _first_present(page, PASS_SELECTORS) is None:
                    self._settle(page)
                    return
            except Exception:
                return
            page.wait_for_timeout(step)
            waited += step

    def login(self, url: str, username: str, password: str,
              code: Optional[str] = None) -> LoginOutcome:
        """Sign in by driving the page's own form. Used when render is on."""
        return _BrowserLogin(self).run(url, username, password, code)

    def summary(self) -> Dict:
        return {
            "rendered": self.rendered,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "cache_entries": len(self.cache) if self.cache else 0,
        }

class LoginOutcome:
    __slots__ = ("ok", "reason", "cookies", "needs_totp", "needs_captcha", "url")

    def __init__(self, ok, reason="", cookies=None, needs_totp=False,
                 needs_captcha=False, url=""):
        self.ok = ok
        self.reason = reason
        self.cookies = cookies or []
        self.needs_totp = needs_totp
        self.needs_captcha = needs_captcha
        self.url = url


USER_SELECTORS = (
    'input[name="username"]', 'input[name="user"]', 'input[name="login"]',
    'input[name="email"]', 'input[type="email"]', 'input[name="account"]',
    'input[type="text"]', 'input:not([type])',
)
PASS_SELECTORS = (
    'input[type="password"]', 'input[name="password"]', 'input[name="passwd"]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]', 'input[type="submit"]',
    'form button', 'button',
)

TOTP_SELECTORS = (
    'input[name="otp"]', 'input[name="code"]', 'input[name="token"]',
    'input[autocomplete="one-time-code"]', 'input[name="totp"]',
)


def _first_present(page, selectors):
    for selector in selectors:
        try:
            if page.query_selector(selector) is not None:
                return selector
        except Exception:
            continue
    return None


class _BrowserLogin:
    """Drives a real login form, for sites whose forms only exist after JS runs.

    A single-page app usually builds its form in the browser and submits JSON
    to an API, so posting the form over plain HTTP cannot work. Filling it in
    the page and clicking submit can.
    """

    def __init__(self, renderer: "Renderer"):
        self.renderer = renderer

    def run(self, url: str, username: str, password: str,
            code: Optional[str] = None) -> LoginOutcome:
        executor = self.renderer._ensure_executor()
        try:
            return executor.submit(
                self.renderer._login_on_worker, url, username, password, code
            ).result()
        except Exception as exc:
            return LoginOutcome(False, "login-failed: %s" % _brief(exc))


def _is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timeout" in text or "exceeded" in text


def _brief(exc: Exception) -> str:
    """A short, safe description: no credentials, no full traceback."""
    name = type(exc).__name__
    message = str(exc).splitlines()[0][:160] if str(exc) else ""
    return "%s: %s" % (name, message) if message else name



def _cookies_from_session(session) -> List[Dict]:
    if session is None:
        return []
    out = []
    for cookie in getattr(session, "cookies", []):
        out.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
        })
    return out
