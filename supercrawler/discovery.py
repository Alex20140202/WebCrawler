from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

from .parser import normalize_url, registrable_domain, same_site

SITEMAP_CONTENT_TYPES = frozenset({
    "application/xml", "text/xml", "application/gzip", "application/x-gzip",
    "application/octet-stream", "text/plain", "application/rss+xml",
})

COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/wp-sitemap.xml",
    "/sitemap.xml.gz",
)

SITEMAP_DIRECTIVE_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

PAGINATION_PATTERNS = (
    re.compile(r"(?:page|paged|p|pg|start|offset|from)=(\d+)", re.IGNORECASE),
    re.compile(r"/(?:page|pg|p)/(\d+)(?:[/?]|$)", re.IGNORECASE),
    re.compile(r"[/_-](?:page|pg|post)[-_](\d+)(?:[/_.-]|$)", re.IGNORECASE),
)

PAGINATION_QUERY_RE = re.compile(r"^[^=]+=(?:page|paged|p|pg|start|offset|from)=(\d+)",
                                 re.IGNORECASE)

NON_PAGE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".rar", ".7z", ".mp3", ".mp4", ".avi", ".mov",
    ".wmv", ".webm", ".css", ".js", ".json", ".rss", ".atom", ".woff",
    ".woff2", ".ttf", ".eot", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
    ".pptx", ".csv", ".dmg", ".exe", ".apk",
)

SKIP_PATH_PREFIXES = (
    "/wp-admin", "/wp-login", "/wp-json", "/administrator", "/admin",
    "/login", "/logout", "/signin", "/signup", "/register", "/cart",
    "/checkout", "/account", "/my-account", "/password", "/search",
    "/feed", "/tag", "/author", "/comment", "/trackback", "/xmlrpc",
)


class SitemapEntry:
    __slots__ = ("loc", "lastmod", "changefreq", "priority")

    def __init__(self, loc, lastmod=None, changefreq=None, priority=None):
        self.loc = loc
        self.lastmod = lastmod
        self.changefreq = changefreq
        self.priority = priority

    def as_dict(self) -> Dict:
        return {
            "loc": self.loc,
            "lastmod": self.lastmod,
            "changefreq": self.changefreq,
            "priority": self.priority,
        }


def find_sitemap_directives(robots_text: str) -> List[str]:
    return [m.group(1).strip() for m in SITEMAP_DIRECTIVE_RE.finditer(robots_text or "")]


def parse_sitemap(xml_text: str) -> Tuple[List[SitemapEntry], List[str]]:
    """Parse a sitemap or sitemap index. Returns (url entries, nested sitemap locs)."""
    if not xml_text or not xml_text.strip():
        return [], []

    if xml_text[:2] == "\x1f\x8b":
        try:
            xml_text = gzip.decompress(xml_text.encode("latin-1")).decode("utf-8", "replace")
        except (OSError, EOFError, UnicodeError):
            return [], []

    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return [], []

    tag = root.tag.split("}")[-1].lower()
    entries: List[SitemapEntry] = []
    nested: List[str] = []

    if tag == "sitemapindex":
        for node in root:
            loc = _child_text(node, "loc")
            if loc:
                nested.append(loc)
        return entries, nested

    if tag == "urlset":
        for node in root:
            if node.tag.split("}")[-1].lower() != "url":
                continue
            loc = _child_text(node, "loc")
            if not loc:
                continue
            entries.append(SitemapEntry(
                loc=loc,
                lastmod=_child_text(node, "lastmod"),
                changefreq=_child_text(node, "changefreq"),
                priority=_child_text(node, "priority"),
            ))
    return entries, nested


def _child_text(node, name: str) -> Optional[str]:
    for child in node:
        if child.tag.split("}")[-1].lower() == name:
            text = (child.text or "").strip()
            if text:
                return text
    return None


def candidate_sitemaps(base_url: str, robots_text: str = "") -> List[str]:
    """Sitemap URLs to try, declared ones first, then well-known locations."""
    parts = urlsplit(base_url)
    root = "%s://%s" % (parts.scheme, parts.netloc)
    ordered: List[str] = []

    for declared in find_sitemap_directives(robots_text):
        absolute = normalize_url(declared, base_url)
        if absolute and absolute not in ordered:
            ordered.append(absolute)

    for path in COMMON_SITEMAP_PATHS:
        candidate = root + path
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def is_crawlable_url(url: str, allow_search: bool = False) -> bool:
    """Whether a discovered URL is worth fetching.

    `allow_search` relaxes the /search rule. Link-following keeps it on so a
    site's own search results cannot trap the crawl in a loop, while the agent
    may still deliberately request a search URL it reasoned about.
    """
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    if path.endswith(NON_PAGE_SUFFIXES):
        return False
    prefixes = SKIP_PATH_PREFIXES
    if allow_search:
        prefixes = tuple(p for p in SKIP_PATH_PREFIXES if p != "/search")
    if any(path.startswith(prefix) for prefix in prefixes):
        return False
    return True


def pagination_key(url: str) -> Optional[str]:
    """Return the paginating fragment if the URL looks like page 2 or later."""
    parts = urlsplit(url)
    for source in (parts.path, parts.query):
        for pattern in PAGINATION_PATTERNS:
            match = pattern.search(source)
            if match and int(match.group(1)) > 1:
                return match.group(0)
    return None


def strip_pagination(url: str) -> Optional[str]:
    """Canonical form of a paginated URL with its page number removed."""
    parts = urlsplit(url)
    for target in (parts.path, parts.query):
        for pattern in PAGINATION_PATTERNS:
            if not pattern.search(target):
                continue
            cleaned = pattern.sub(lambda m: m.group(0).replace(m.group(1), "1"), target)
            if cleaned == target:
                continue
            if target is parts.path:
                return urlunsplit((parts.scheme, parts.netloc, cleaned, parts.query, ""))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, cleaned, ""))
    return None


def strip_tracking_params(url: str) -> str:
    noisy = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "msclkid", "ref", "referrer", "_ga", "yclid",
    }
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = []
    for pair in parts.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in noisy:
            continue
        kept.append(pair)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       "&".join(kept), parts.query if not kept else ""))


def looks_js_rendered(html_text: str, link_count: int, word_count: int) -> bool:
    """Heuristic: few links and words but heavy scripts usually means client-side rendering."""
    if not html_text:
        return False
    scripts = len(re.findall(r"<script\b", html_text, re.IGNORECASE))
    if word_count > 150 or link_count > 15:
        return False
    if scripts >= 3 and link_count <= 5 and word_count <= 50:
        return True
    if word_count <= 10 and scripts >= 1 and link_count <= 3:
        return True
    return False


def estimate_from_links(urls: Sequence[str]) -> Dict:
    """Crude size estimate for a plan, used to set a sensible page budget."""
    total = len(urls)
    if total == 0:
        return {"known_urls": 0, "est_pages": 0, "source": "unknown"}
    if total > 500:
        return {"known_urls": total, "est_pages": total, "source": "sitemap"}
    # BFS from a single seed usually surfaces a small multiple of the seed's
    # out-degree, not of the total, so keep the multiplier modest.
    estimate = min(total * 3, total + 40)
    return {"known_urls": total, "est_pages": estimate, "source": "links"}


def recommend_presets(estimate: Dict) -> Dict[str, int]:
    """Suggest depth / page budget / speed from an estimated site size."""
    est = estimate.get("est_pages", 0)
    if est <= 50:
        return {"max_depth": 2, "max_pages": 100, "concurrency": 8, "delay": 0.3}
    if est <= 500:
        return {"max_depth": 4, "max_pages": 1000, "concurrency": 8, "delay": 0.5}
    if est <= 5000:
        return {"max_depth": 6, "max_pages": 10000, "concurrency": 12, "delay": 0.7}
    return {"max_depth": 10, "max_pages": 0, "concurrency": 12, "delay": 1.0}


class SiteProbe:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.host = (urlsplit(base_url).hostname or "").lower()
        self.robots_text = ""
        self.robots_exists = False
        self.sitemap_sources: List[str] = []
        self.sitemap_entries: List[SitemapEntry] = []
        self.sitemap_count = 0
        self.truncated = False
        self.notes: List[str] = []

    def urls(self, limit: Optional[int] = None) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        for entry in self.sitemap_entries:
            url = normalize_url(entry.loc, self.base_url)
            if not url or url in seen:
                continue
            if not same_site(url, self.base_url):
                continue
            if not is_crawlable_url(url):
                continue
            seen.add(url)
            out.append(url)
            if limit and len(out) >= limit:
                break
        return out

    def as_dict(self) -> Dict:
        return {
            "base_url": self.base_url,
            "host": self.host,
            "domain": registrable_domain(self.host),
            "robots_exists": self.robots_exists,
            "sitemap_sources": list(self.sitemap_sources),
            "sitemap_entries": self.sitemap_count,
            "urls_usable": len(self.urls()),
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


def probe_site(
    fetch_text,
    base_url: str,
    max_sitemaps: int = 8,
    max_entries: int = 100000,
    logger=None,
) -> SiteProbe:
    """Discover sitemaps for a site and collect their URLs.

    fetch_text(url) must return text or None; injected so this stays testable.
    """
    probe = SiteProbe(base_url)
    parts = urlsplit(base_url)
    root = "%s://%s" % (parts.scheme, parts.netloc)

    def log(message):
        if logger:
            logger(message)

    robots_text = fetch_text(root + "/robots.txt")
    if robots_text:
        probe.robots_text = robots_text
        probe.robots_exists = True

    queue = candidate_sitemaps(base_url, probe.robots_text)
    visited: Set[str] = set()
    entries: List[SitemapEntry] = []

    while queue and len(probe.sitemap_sources) < max_sitemaps:
        sitemap_url = queue.pop(0)
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)

        text = fetch_text(sitemap_url)
        if not text:
            continue

        found, nested = parse_sitemap(text)
        if found:
            probe.sitemap_sources.append(sitemap_url)
            entries.extend(found)
            log("  sitemap %s -> %d url(s)" % (sitemap_url, len(found)))
        elif nested:
            probe.sitemap_sources.append(sitemap_url)
            log("  sitemap index %s -> %d child map(s)" % (sitemap_url, len(nested)))
            for child in nested:
                absolute = normalize_url(child, sitemap_url)
                if absolute and absolute not in visited and absolute not in queue:
                    queue.append(absolute)

        if len(entries) >= max_entries:
            probe.truncated = True
            probe.notes.append("sitemap entry cap (%d) hit" % max_entries)
            break

    probe.sitemap_entries = entries
    probe.sitemap_count = len(entries)
    if not probe.sitemap_sources:
        probe.notes.append("no sitemap found; will rely on link discovery")
    return probe