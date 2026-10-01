from .config import PRESETS, CrawlConfig, get_preset
from .crawler import Crawler, crawl
from .fetcher import Fetcher, Throttle
from .parser import (
    extract_emails,
    extract_links,
    extract_page,
    normalize_url,
    registrable_domain,
    same_site,
    word_frequencies,
)
from .report import write_csv, write_html, write_json, write_jsonl, write_reports

__version__ = "1.0.0"

__all__ = [
    "CrawlConfig",
    "PRESETS",
    "get_preset",
    "Crawler",
    "crawl",
    "Fetcher",
    "Throttle",
    "normalize_url",
    "registrable_domain",
    "same_site",
    "extract_page",
    "extract_links",
    "extract_emails",
    "word_frequencies",
    "write_json",
    "write_jsonl",
    "write_csv",
    "write_html",
    "write_reports",
]