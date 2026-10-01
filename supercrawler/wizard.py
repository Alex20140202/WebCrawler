from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .config import CrawlConfig, get_preset
from .discovery import candidate_sitemaps, probe_site

Prompter = Callable[[str], str]

# Data-collection goals, each implying a sensible default extract/exclude setup.
GOALS = {
    "content": {
        "label": "site content and structure",
        "include": None,
        "exclude": [r"\.(pdf|zip|gz|mp4|mp3|jpg|png|gif|svg|css|js)$"],
    },
    "seo": {
        "label": "SEO audit (titles, meta, headings, status codes)",
        "include": None,
        "exclude": [r"\.(pdf|zip|gz|mp4|mp3|jpg|png|gif|svg|css|js)$"],
    },
    "links": {
        "label": "link graph and internal structure",
        "include": None,
        "exclude": [r"\.(pdf|zip|gz|mp4|mp3|jpg|png|gif|svg|css|js)$"],
    },
    "contacts": {
        "label": "contact details and emails",
        "include": r"/(contact|about|team|company|impressum|kontakt|privacy|legal)",
        "exclude": [r"\.(pdf|zip|gz|mp4|mp3|jpg|png|gif|svg|css|js)$"],
    },
}

SCOPES = ("whole site", "a section/path", "single page", "custom URL list")


def _default_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _ask(prompt: str, default: str = "", prompter: Prompter = _default_input) -> str:
    suffix = " [%s]" % default if default else ""
    answer = (prompter("%s%s: " % (prompt, suffix)) or "").strip()
    return answer or default


def _ask_yes_no(prompt: str, default: bool, prompter: Prompter = _default_input) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = (prompter("%s (%s): " % (prompt, hint)) or "").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "1", "true")


def _ask_choice(prompt: str, options: List[str], default_index: int = 0,
                prompter: Prompter = _default_input) -> int:
    print(prompt)
    for index, option in enumerate(options, start=1):
        marker = "*" if index - 1 == default_index else " "
        print("  %s %d) %s" % (marker, index, option))
    while True:
        raw = (prompter("choice [%d]: " % (default_index + 1)) or "").strip()
        if not raw:
            return default_index
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("  please enter a number between 1 and %d" % len(options))


def _ask_int(prompt: str, default: int, prompter: Prompter = _default_input) -> int:
    while True:
        raw = (prompter("%s [%d]: " % (prompt, default)) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            pass
        print("  please enter a whole number (%d for the default)" % default)


def _ask_float(prompt: str, default: float, prompter: Prompter = _default_input) -> float:
    while True:
        raw = (prompter("%s [%s]: " % (prompt, default)) or "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
        print("  please enter a number >= 0")


def _ask_list(prompt: str, prompter: Prompter = _default_input) -> List[str]:
    print(prompt)
    print("  (one per line, blank line to finish)")
    items = []
    while True:
        raw = prompter("  > ")
        if raw is None:
            break
        raw = raw.strip()
        if not raw:
            break
        items.append(raw)
    return items


def normalize_site_input(raw: str) -> str:
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = "https://" + candidate
    return candidate.rstrip("/")


def build_plan(
    fetcher_text: Optional[Callable[[str], Optional[str]]] = None,
    prompter: Prompter = _default_input,
) -> Dict:
    """Ask the questions needed to run a whole-site crawl.

    Returns a dict with 'config', 'base_url', and 'plan'. With no fetcher the
    sitemap probe is skipped, so this stays unit-testable without network.
    """
    print("=" * 58)
    print("  SuperCrawler setup")
    print("=" * 58)

    print("\nStep 1/6 - what should we crawl?")
    base_raw = _ask("site URL or domain", "", prompter)
    if not base_raw:
        raise ValueError("a site URL is required")
    base_url = normalize_site_input(base_raw)
    if not base_url:
        raise ValueError("could not understand that URL: %r" % base_raw)

    scope_index = _ask_choice("Step 2/6 - how much of it?", list(SCOPES), 0, prompter)
    scope = SCOPES[scope_index]

    goal_index = _ask_choice(
        "Step 3/6 - what are you collecting?",
        [GOALS[key]["label"] for key in GOALS],
        0,
        prompter,
    )
    goal_key = list(GOALS)[goal_index]
    goal = GOALS[goal_key]

    extra_seeds: List[str] = []
    extra_excludes: List[str] = []
    include_patterns = [goal["include"]] if goal["include"] else []

    if scope == "a section/path":
        path = _ask("path to crawl (e.g. /docs)", "/", prompter)
        if not path.startswith("/"):
            path = "/" + path
        base_url = base_url + path.rstrip("/")
    elif scope == "custom URL list":
        extra_seeds = _ask_list("  extra starting URLs", prompter)
    elif scope == "single page":
        include_patterns = [r"^%s$" % _escape(base_url)]

    print("\nStep 4/6 - limits")
    limits = [
        "unlimited (whole site, stops when links run out)",
        "up to 100 pages (quick sample)",
        "up to 1,000 pages",
        "up to 10,000 pages",
    ]
    limit_index = _ask_choice("  page budget", limits, 0, prompter)
    max_pages = [0, 100, 1000, 10000][limit_index]
    default_depth = 12 if max_pages == 0 else 6
    max_depth = _ask_int("  link depth (0 = seeds only, -1 = unlimited)",
                         default_depth, prompter)
    max_pages = _ask_int("  max pages (0 = unlimited)", max_pages, prompter)

    preset_index = _ask_choice(
        "\nStep 5/6 - crawl speed",
        ["polite (slowest, safest)", "balanced (recommended)", "fast (loudest)"],
        1,
        prompter,
    )
    preset_name = ["polite", "balanced", "fast"][preset_index]
    preset = get_preset(preset_name)
    delay = _ask_float("  seconds between requests per host", preset.delay, prompter)
    concurrency = _ask_int("  parallel requests", preset.concurrency, prompter)
    per_host = _ask_int("  parallel requests per host", preset.per_host_concurrency, prompter)

    user_agent = _ask("  User-Agent", preset.user_agent, prompter)

    print("\nStep 6/6 - extras")
    respect_robots = _ask_yes_no("  obey robots.txt?", True, prompter)
    use_sitemap = _ask_yes_no("  use sitemap.xml to find pages?", True, prompter)
    dedupe = _ask_yes_no("  skip duplicate content?", True, prompter)
    save_raw = _ask_yes_no("  save raw HTML for each page?", False, prompter)
    follow_external = _ask_yes_no("  follow links to other domains?", False, prompter)
    output_dir = _ask("  output directory", "output", prompter)
    extra_excludes = _ask_list("  URL patterns to skip (regex, optional)", prompter)

    seeds = [base_url] + extra_seeds
    excludes = list(goal["exclude"]) + extra_excludes

    config = CrawlConfig(
        seeds=tuple(seeds),
        max_depth=max_depth,
        max_pages=max_pages,
        concurrency=concurrency,
        per_host_concurrency=per_host,
        delay=delay,
        user_agent=user_agent,
        respect_robots=respect_robots,
        use_sitemap=use_sitemap,
        dedupe_content=dedupe,
        save_html=save_raw,
        follow_external=follow_external,
        include_patterns=tuple(include_patterns) if include_patterns else None,
        exclude_patterns=tuple(excludes) if excludes else None,
        output_dir=output_dir,
    )

    plan: Dict = {
        "base_url": base_url,
        "scope": scope,
        "goal": goal_key,
        "sitemap_candidates": candidate_sitemaps(base_url),
        "probed": None,
    }

    if fetcher_text:
        print("\nprobing for sitemaps...")
        probe = probe_site(fetcher_text, base_url, logger=print)
        urls = probe.urls(limit=max_pages if max_pages > 0 else None)
        plan["probed"] = {
            "sitemap_sources": probe.sitemap_sources,
            "urls_found": len(urls),
            "sample": urls[:10],
        }
        print("  found %d url(s) in %d sitemap(s)"
              % (len(urls), len(probe.sitemap_sources)))

    print("\n" + "=" * 58)
    print("  plan ready")
    print("=" * 58)
    print("  target      : %s" % base_url)
    print("  scope       : %s" % scope)
    print("  collecting  : %s" % goal["label"])
    if max_depth < 0:
        depth_label = "unlimited"
    elif max_depth == 0:
        depth_label = "0 (seed pages only)"
    else:
        depth_label = str(max_depth)
    print("  depth       : %s" % depth_label)
    print("  max pages   : %s" % ("unlimited" if max_pages <= 0 else max_pages))
    print("  speed       : %s (concurrency %d, delay %ss, per-host %d)"
          % (preset_name, concurrency, delay, per_host))
    print("  robots.txt  : %s" % ("obeyed" if respect_robots else "ignored"))
    print("  sitemap     : %s" % ("used" if use_sitemap else "skipped"))
    print("  output      : %s/" % output_dir)
    if excludes:
        print("  skipping    : %s" % ", ".join(excludes[:4]))
    if not respect_robots:
        print("\n  NOTE: robots.txt is disabled. Only crawl sites you own or are")
        print("        authorized to crawl.")
    print("=" * 58)

    return {"config": config, "base_url": base_url, "plan": plan}


def _escape(text: str) -> str:
    import re
    return re.escape(text)