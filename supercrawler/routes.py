from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .parser import normalize_url, same_site

# Route literal shapes seen across common SPA routers. Each pattern is
# (compiled regex, kind) and must capture the path in group 1.
ROUTE_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    # route('/x'), register('/x'), add('/x'), on('/x')
    (re.compile(r"\b(?:route|register|addRoute|add_route|navigate|go|push|redirect)\s*\(\s*['\"]([^'\"]{1,200})['\"]"), "call"),
    # path: '/x'  (Vue Router, Angular, React Router objects, config arrays)
    (re.compile(r"\bpath\s*:\s*['\"]([^'\"]{1,200})['\"]"), "path"),
    # <Route path="/x" element={...} />
    (re.compile(r"<Route[^>]*\bpath\s*=\s*['\"]([^'\"]{1,200})['\"]", re.IGNORECASE), "jsx"),
    # href="#/x" in markup or in JS template strings
    (re.compile(r"href\s*=\s*['\"]#([^'\"]{0,200})['\"]"), "hash-href"),
    # location.hash = '#/x'
    (re.compile(r"location\.hash\s*=\s*['\"]#([^'\"]{0,200})['\"]"), "hash-assign"),
    # '#/x' used as a route string inside a config block
    (re.compile(r"['\"]#(/[A-Za-z0-9_\-/.]{0,180})['\"]"), "hash-literal"),
    # Next.js / Nuxt style page files: import x from './pages/about'
    (re.compile(r"['\"](?:\./|\.\./)*(?:pages|app)/([A-Za-z0-9_\-/]+?)(?:/page)?['\"]"), "file-route"),
)

# Markers that mean the route takes parameters and cannot be fetched as-is.
DYNAMIC_MARKERS = (":", "*", "(", "<", "[", "]", "+", "$")
DYNAMIC_SKIP_PREFIX = ("http://", "https://", "//", "data:", "mailto:")

STATIC_FILE_RE = re.compile(
    r"\.(js|mjs|cjs|css|map|json|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|"
    r"pdf|zip|gz|tgz|tar|rar|7z|mp3|mp4|avi|mov|wmv|webm|csv|docx?|xlsx?|"
    r"pptx?|dmg|exe|apk|xml|rss|atom|txt)$",
    re.IGNORECASE,
)

SCRIPT_SRC_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+\.m?js(?:\?[^"']*)?)["']""",
    re.IGNORECASE,
)

IMPORT_RE = re.compile(
    r"""(?:^|[\s;])import\s+(?:[^'"]*?\bfrom\s*)?["']([^"']+\.m?js)["']""",
    re.MULTILINE,
)
DYNAMIC_IMPORT_RE = re.compile(
    r"""import\s*\(\s*["']([^"']+\.m?js)["']\s*\)""", re.IGNORECASE
)


class RouteCandidate:
    __slots__ = ("path", "kind", "source", "dynamic", "params")

    def __init__(self, path: str, kind: str, source: str,
                 dynamic: bool = False, params: Optional[Sequence[str]] = None):
        self.path = path
        self.kind = kind
        self.source = source
        self.dynamic = dynamic
        self.params = list(params or [])

    def as_dict(self) -> Dict:
        return {"path": self.path, "kind": self.kind, "source": self.source,
                "dynamic": self.dynamic, "params": self.params}


def looks_like_route(value: str) -> bool:
    if not value or len(value) > 200:
        return False
    lowered = value.lower()
    if lowered.startswith(DYNAMIC_SKIP_PREFIX):
        return False
    if STATIC_FILE_RE.search(value):
        return False
    if not value.startswith("/"):
        return False
    if value == "/":
        return True
    # Skip obvious non-paths: bare punctuation, numbers, regex fragments.
    if re.fullmatch(r"[\d\W]+", value):
        return False
    return True


def param_names(path: str) -> List[str]:
    found = re.findall(r":([A-Za-z0-9_]+)", path)
    found += re.findall(r"\[([A-Za-z0-9_]+)\]", path)
    found += re.findall(r"<([A-Za-z0-9_]+)>", path)
    found += re.findall(r"\$([A-Za-z0-9_]+)", path)
    return found


def is_dynamic(path: str) -> bool:
    if "*" in path or "(" in path:
        return True
    return bool(param_names(path))


def collect_js_urls(html: str, base_url: str) -> List[str]:
    """Every JS asset a page pulls in, including module preloads."""
    soup = BeautifulSoup(html, "html.parser")
    out: List[str] = []
    for node in soup.find_all(["script", "link"]):
        if node.name == "script":
            src = node.get("src")
        else:
            rels = [str(r).lower() for r in (node.get("rel") or [])]
            if not {"modulepreload", "preload", "prefetch"} & set(rels):
                continue
            src = node.get("href")
        if not src:
            continue
        url = normalize_url(src, base_url)
        if url and url not in out:
            out.append(url)
    for match in SCRIPT_SRC_RE.finditer(html):
        url = normalize_url(match.group(1), base_url)
        if url and url not in out:
            out.append(url)
    return out


def collect_imports(js_text: str, base_url: str) -> List[str]:
    """Same-origin JS modules a bundle imports directly."""
    out: List[str] = []
    for pattern in (IMPORT_RE, DYNAMIC_IMPORT_RE):
        for match in pattern.finditer(js_text):
            spec = match.group(1)
            url = normalize_url(spec, base_url)
            if url and same_site(url, base_url) and url not in out:
                out.append(url)
    return out


def extract_routes_from_js(js_text: str, source_url: str) -> List[RouteCandidate]:
    out: List[RouteCandidate] = []
    seen: Set[str] = set()
    for pattern, kind in ROUTE_PATTERNS:
        for match in pattern.finditer(js_text):
            raw = (match.group(1) or "").strip()
            if not looks_like_route(raw):
                continue
            path = raw if raw.startswith("/") else "/" + raw
            path = re.sub(r"\{[^}]*\}", "", path)  # template placeholders
            path = re.sub(r"/{2,}", "/", path)
            if not looks_like_route(path) or path in seen:
                continue
            seen.add(path)
            out.append(RouteCandidate(path, kind, source_url,
                                      is_dynamic(path), param_names(path)))
    return out


def extract_routes_from_html(html: str, page_url: str) -> List[RouteCandidate]:
    return extract_routes_from_js(html, page_url)


class RouteReport:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.static: List[RouteCandidate] = []
        self.dynamic: List[RouteCandidate] = []
        self.scripts_scanned: List[str] = []
        self.scripts_failed: List[str] = []
        self.notes: List[str] = []

    @property
    def total(self) -> int:
        return len(self.static) + len(self.dynamic)

    def add(self, candidate: RouteCandidate) -> None:
        bucket = self.dynamic if candidate.dynamic else self.static
        if any(c.path == candidate.path for c in bucket):
            return
        bucket.append(candidate)

    def as_dict(self) -> Dict:
        return {
            "base_url": self.base_url,
            "scripts_scanned": len(self.scripts_scanned),
            "scripts_failed": len(self.scripts_failed),
            "static_routes": len(self.static),
            "dynamic_routes": len(self.dynamic),
            "static": [c.path for c in self.static],
            "dynamic": [{"path": c.path, "params": c.params, "kind": c.kind}
                        for c in self.dynamic],
            "notes": list(self.notes),
        }


def discover_routes(
    fetch_text: Callable[[str], Optional[str]],
    page_url: str,
    max_scripts: int = 30,
    logger: Optional[Callable[[str], None]] = None,
) -> RouteReport:
    """Find SPA routes by reading the page's JavaScript.

    For a hash-routed app every route is the same HTTP resource with a different
    fragment, so link extraction cannot find them. The route table lives in the
    bundle instead, which is what this reads.
    """
    report = RouteReport(page_url)
    log = logger or (lambda message: None)

    html = fetch_text(page_url)
    if not html:
        report.notes.append("could not fetch the page")
        return report

    for candidate in extract_routes_from_html(html, page_url):
        report.add(candidate)

    scripts = collect_js_urls(html, page_url)
    if not scripts:
        report.notes.append("page references no JavaScript files")
        return report

    queue = list(scripts)
    seen: Set[str] = set()
    while queue and len(report.scripts_scanned) < max_scripts:
        script_url = queue.pop(0)
        if script_url in seen:
            continue
        seen.add(script_url)

        text = fetch_text(script_url)
        if not text:
            report.scripts_failed.append(script_url)
            continue
        report.scripts_scanned.append(script_url)

        for candidate in extract_routes_from_js(text, script_url):
            report.add(candidate)

        if len(report.scripts_scanned) >= max_scripts:
            break
        # One level of imports is enough to reach a route table that lives in
        # its own module, without walking an entire dependency graph.
        for imported in collect_imports(text, script_url)[:8]:
            if imported not in seen:
                queue.append(imported)

    if report.dynamic:
        report.notes.append(
            "%d route(s) take parameters and need values enumerated before they "
            "can be fetched" % len(report.dynamic)
        )
    if len(report.scripts_failed):
        report.notes.append("%d script(s) could not be read"
                            % len(report.scripts_failed))
    log("routes: %d static, %d dynamic from %d script(s)"
        % (len(report.static), len(report.dynamic), len(report.scripts_scanned)))
    return report


def route_urls(report: RouteReport, include_dynamic: bool = False,
               base_path: str = "") -> List[str]:
    """Turn discovered routes into fetchable URLs.

    With a hash-routed app the fragment is the only thing that differs, so the
    URL is identical up to '#/route'. Those still have to be rendered, because
    fetching them server-side returns the same shell for every route.
    """
    out: List[str] = []
    for candidate in report.static + (report.dynamic if include_dynamic else []):
        url = urljoin(report.base_url, base_path + candidate.path)
        normalized = normalize_url(url)
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def hash_variants(base_url: str, routes: Sequence[str]) -> List[str]:
    """URLs that differ only by fragment, for hash-routed SPAs."""
    out: List[str] = []
    for path in routes:
        if not path.startswith("/"):
            continue
        url = "%s#%s" % (base_url.rstrip("/"), path)
        if url not in out:
            out.append(url)
    return out