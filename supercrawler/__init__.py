from .config import PRESETS, CrawlConfig, get_preset
from .agent import ACTION_FEED, ACTION_LOGIN, ACTION_NEXT, ACTION_PAGINATE, Agent
from .auth import AuthError, Authenticator, LoginResult, describe_auth
from .crawler import Crawler, crawl
from .discovery import (
    SiteProbe,
    candidate_sitemaps,
    estimate_from_links,
    is_crawlable_url,
    looks_js_rendered,
    pagination_key,
    parse_sitemap,
    probe_site,
    recommend_presets,
    strip_tracking_params,
)
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
from .interact import Interactor
from .report import write_csv, write_html, write_json, write_jsonl, write_reports
from .scanner import Finding, PageSignals, build_paged_urls, expand_search_form, scan_page
from .state import CrawlState, load_state, save_state
from .wizard import build_plan

__version__ = "4.0.0"

__all__ = [
    "CrawlConfig",
    "PRESETS",
    "get_preset",
    "Crawler",
    "crawl",
    "probe_site",
    "SiteProbe",
    "parse_sitemap",
    "candidate_sitemaps",
    "estimate_from_links",
    "recommend_presets",
    "is_crawlable_url",
    "pagination_key",
    "strip_tracking_params",
    "looks_js_rendered",
    "build_plan",
    "Agent",
    "ACTION_PAGINATE",
    "ACTION_NEXT",
    "ACTION_FEED",
    "ACTION_LOGIN",
    "Interactor",
    "scan_page",
    "PageSignals",
    "Finding",
    "build_paged_urls",
    "expand_search_form",
    "Authenticator",
    "LoginResult",
    "AuthError",
    "describe_auth",
    "CrawlState",
    "save_state",
    "load_state",
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