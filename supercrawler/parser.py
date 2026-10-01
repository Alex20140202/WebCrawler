from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "about:", "ftp:", "file:")

MULTI_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp",
    "com.cn", "net.cn", "org.cn", "gov.cn", "com.au", "net.au", "org.au",
    "com.br", "com.tw", "com.hk", "com.sg", "co.kr", "co.in", "co.za",
    "com.mx", "com.ar", "com.tr", "com.sg", "com.pl", "com.ua", "com.vn",
})

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")

STOPWORDS = frozenset("""
a an and are as at be but by for from has have he her his i if in is it its of on or
she that the their them then there these they this to was were will with you your our
not no so such than too very can just don should now about after before above below
over under again once here when where why how all any both each few more most other
some only own same s t don
""".split())


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Resolve against base and canonicalize. Returns None for non-crawlable URLs."""
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith("#"):
        return None
    lowered = url.lower()
    if lowered.startswith(SKIP_SCHEMES):
        return None

    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None

    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = "%s:%d" % (host, port)
    else:
        netloc = host

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path == "":
        path = "/"

    try:
        return urlunsplit((scheme, netloc, path, parts.query, ""))
    except ValueError:
        return None


def registrable_domain(host: Optional[str]) -> str:
    """Best-effort eTLD+1 without the public suffix list dependency."""
    if not host:
        return ""
    host = host.lower().strip(".")
    labels = host.split(".")
    if len(labels) < 2:
        return host
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(url: str, other: str) -> bool:
    a = registrable_domain(urlsplit(url).hostname)
    b = registrable_domain(urlsplit(other).hostname)
    return bool(a) and a == b


def is_http_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def extract_emails(html: str, limit: int = 50) -> List[str]:
    found = {m.group(0).lower().rstrip(".") for m in EMAIL_RE.finditer(html)}
    return sorted(found)[:limit]


def extract_links(html: str, base_url: str, follow_nofollow: bool = False) -> Dict[str, List[str]]:
    """Split outgoing links into internal, external, and nofollow buckets."""
    soup = BeautifulSoup(html, "html.parser")
    internal: List[str] = []
    external: List[str] = []
    nofollow: List[str] = []

    base_href = None
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_href = normalize_url(base_tag["href"], base_url)
    resolve_from = base_href or base_url

    for anchor in soup.find_all("a", href=True):
        rel = anchor.get("rel") or []
        is_nofollow = "nofollow" in [str(r).lower() for r in rel]
        target = normalize_url(anchor["href"], resolve_from)
        if target is None:
            continue
        if is_nofollow and not follow_nofollow:
            if target not in nofollow:
                nofollow.append(target)
            continue
        bucket = internal if same_site(target, base_url) else external
        if target not in bucket:
            bucket.append(target)

    return {"internal": internal, "external": external, "nofollow": nofollow}


def extract_page(html: str, url: str) -> Dict[str, object]:
    """Extract SEO/content signals from an HTML document."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif soup.title:
        title = soup.title.get_text(strip=True)

    meta = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").lower().strip()
        content = (tag.get("content") or "").strip()
        if key and content and key not in meta:
            meta[key] = content

    description = meta.get("description", "")
    keywords = [k.strip() for k in meta.get("keywords", "").split(",") if k.strip()]

    canonical = ""
    for tag in soup.find_all("link", rel=True):
        rels = [str(r).lower() for r in (tag.get("rel") or [])]
        if "canonical" in rels and tag.get("href"):
            canonical = normalize_url(tag["href"], url) or ""
            break

    headings = []
    for level in ("h1", "h2", "h3"):
        for tag in soup.find_all(level):
            text = tag.get_text(" ", strip=True)
            if text:
                headings.append({"level": int(level[1]), "text": text[:300]})

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    words = WORD_RE.findall(text.lower())
    word_count = len(words)

    keywords_top = [w for w, _ in _top_terms(words, 12)]

    images = soup.find_all("img")
    image_alts = [i.get("alt", "").strip() for i in images]
    missing_alt = sum(1 for i in images if not i.get("alt"))

    return {
        "title": title[:300],
        "title_length": len(title),
        "meta_description": description[:500],
        "meta_keywords": keywords,
        "canonical": canonical,
        "lang": (soup.html.get("lang") if soup.html else None),
        "headings": headings,
        "h1_count": sum(1 for h in headings if h["level"] == 1),
        "word_count": word_count,
        "text_preview": text[:400],
        "top_terms": keywords_top,
        "image_count": len(images),
        "images_missing_alt": missing_alt,
        "has_viewport": "viewport" in meta,
        "has_og": any(k.startswith("og:") for k in meta),
        "has_twitter_card": "twitter:card" in meta,
    }


def _top_terms(words: List[str], limit: int) -> List[Tuple[str, int]]:
    counts = {}
    for word in words:
        if len(word) < 3 or word in STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:limit]


def word_frequencies(html: str, limit: int = 15) -> List[Tuple[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    words = WORD_RE.findall(soup.get_text(" ").lower())
    return _top_terms(words, limit)