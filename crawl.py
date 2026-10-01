#!/usr/bin/env python3
"""Command line interface for the SuperCrawler package."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

import os

from supercrawler import PRESETS, get_preset, crawl
from supercrawler.crawler import Crawler, seeds_first
from supercrawler.fetcher import Fetcher
from supercrawler.routes import discover_routes
from supercrawler.wizard import build_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl.py",
        description="Polite, concurrent, robots-aware web crawler with reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("seeds", nargs="*", help="start URLs (https://example.com)")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="balanced",
                        help="named concurrency/depth profile")
    parser.add_argument("--max-depth", type=int, help="maximum link depth")
    parser.add_argument("--max-pages", type=int, help="maximum pages to fetch")
    parser.add_argument("--concurrency", type=int, help="parallel requests")
    parser.add_argument("--per-host", type=int, help="parallel requests per host")
    parser.add_argument("--delay", type=float, help="seconds between requests per host")
    parser.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    parser.add_argument("--max-bytes", type=int, help="max bytes to read per response")
    parser.add_argument("--retries", type=int, help="retries for failed/transient requests")
    parser.add_argument("--user-agent", help="User-Agent header to send")
    parser.add_argument("-d", "--domain", action="append", dest="domains",
                        metavar="DOMAIN", help="restrict to domain (repeatable)")
    parser.add_argument("-b", "--block-domain", action="append", dest="blocked_domains",
                        metavar="DOMAIN", help="skip domain (repeatable)")
    parser.add_argument("--include", action="append", dest="include_patterns",
                        metavar="REGEX", help="only crawl URLs matching regex")
    parser.add_argument("--exclude", action="append", dest="exclude_patterns",
                        metavar="REGEX", help="skip URLs matching regex")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="do not read robots.txt (use only where permitted)")
    parser.add_argument("--follow-external", action="store_true",
                        help="follow links to other domains")
    parser.add_argument("--follow-nofollow", action="store_true",
                        help="also follow rel=nofollow links")
    parser.add_argument("--save-html", action="store_true", help="save raw HTML per page")
    parser.add_argument("-o", "--output", default="output", help="output directory")
    parser.add_argument("--html-out", help="custom path for the HTML report")
    parser.add_argument("--csv-out", help="custom path for the CSV report")
    parser.add_argument("--no-reports", action="store_true", help="do not write report files")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--explain", action="store_true",
                        help="always print why the crawl reached the pages it did "
                             "(printed automatically for 1-2 page crawls)")
    parser.set_defaults(interaction_mode=None)

    smart = parser.add_argument_group("smart whole-site mode")
    smart.add_argument("--smart", action="store_true",
                       help="use sitemap.xml to find every page, then crawl them all")
    smart.add_argument("--wizard", action="store_true",
                       help="answer questions interactively to build the crawl plan")
    smart.add_argument("--plan-only", action="store_true",
                       help="with --smart: probe and print the plan, then exit")
    smart.add_argument("--state-file", metavar="PATH",
                       help="save/resume crawl progress across runs")
    smart.add_argument("--no-sitemap", action="store_true",
                       help="skip sitemap discovery even in smart mode")
    smart.add_argument("--keep-duplicates", action="store_true",
                       help="do not collapse pages with identical content")
    smart.add_argument("--keep-params", action="store_true",
                       help="keep tracking parameters such as utm_*")
    smart.add_argument("--max-sitemaps", type=int, help="how many sitemap files to read")
    smart.add_argument("--no-resume", action="store_true",
                       help="start fresh even if the state file exists")

    agent_group = parser.add_argument_group("mid-crawl decisions")
    agent_group.add_argument("--ask", dest="interaction_mode",
                             action="store_const", const="ask",
                             help="ask me when the crawler hits an ambiguous choice")
    agent_group.add_argument("--auto", dest="interaction_mode",
                             action="store_const", const="auto",
                             help="act on safe defaults without asking")
    agent_group.add_argument("--never-ask", dest="interaction_mode",
                             action="store_const", const="never",
                             help="never prompt, never auto-expand")
    agent_group.add_argument("--max-questions", type=int,
                             help="how many questions to ask in one run")
    agent_group.add_argument("--no-auto-actions", action="store_true",
                             help="do not act on pagination, feeds or search forms")
    agent_group.add_argument("--max-actions-per-page", type=int,
                             help="cap URLs added per page by the agent")
    agent_group.add_argument("--decisions-file",
                             help="save/load answered decisions (JSON)")

    auth = parser.add_argument_group("login (for sites you are authorized to use)")
    auth.add_argument("--login-url", metavar="URL",
                      help="sign in via this form before crawling")
    auth.add_argument("-u", "--username", metavar="NAME",
                      help="login username")
    auth.add_argument("--password-env", metavar="VAR", default=None,
                      help="environment variable holding the password "
                           "(default SUPERCRAWLER_PASSWORD)")
    auth.add_argument("--password", metavar="VALUE",
                      help="password inline (discouraged: use --password-env, "
                           "it keeps the value out of your shell history)")
    auth.add_argument("--auth-header", metavar="'Name: value'",
                      help="send a fixed header, e.g. 'Authorization: Bearer ...'")
    auth.add_argument("--session-file", metavar="PATH",
                      help="save/reuse cookies so repeat runs skip the login form")
    auth.add_argument("--require-login", action="store_true",
                      help="abort instead of crawling anonymously if login fails")
    auth.add_argument("--check-auth-header", metavar="'Name: value'",
                      help="inspect an auth header locally (shape, expiry, scopes) "
                           "and exit; the value is never sent anywhere")
    spa = parser.add_argument_group("JavaScript-rendered sites")
    spa.add_argument("--render", action="store_true",
                     help="load pages in a headless browser so JS-built content "
                          "and links become visible (needs playwright)")
    spa.add_argument("--render-wait", dest="render_wait_until",
                     choices=["load", "domcontentloaded", "networkidle", "commit"],
                     default=None,
                     help="when to consider a rendered page ready")
    spa.add_argument("--render-settle", type=int, metavar="MS", default=None,
                     help="extra milliseconds to wait for late scripts")
    spa.add_argument("--render-timeout", type=int, metavar="MS", default=None,
                     help="per-page browser timeout")
    spa.add_argument("--render-cache", metavar="PATH", default=None,
                     help="where to keep rendered HTML "
                          "(default <output>/render-cache)")
    spa.add_argument("--no-render-cache", action="store_true",
                     help="re-render every page instead of reusing cached HTML")
    spa.add_argument("--no-extract-routes", action="store_true",
                     help="do not read SPA routes out of JavaScript bundles")
    spa.add_argument("--include-dynamic-routes", action="store_true",
                     help="also queue route templates such as /blog/:slug")
    spa.add_argument("--max-scripts", type=int, metavar="N",
                     help="how many JS files to read for routes")
    spa.add_argument("--routes-only", action="store_true",
                     help="print the routes discovered in the JavaScript, then exit")
    return parser


def config_from_args(args: argparse.Namespace):
    config = get_preset(args.preset)
    if args.smart and args.preset == "balanced":
        config = get_preset("wholesite")
    return config.merged(
        seeds=tuple(args.seeds),
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        concurrency=args.concurrency,
        per_host_concurrency=args.per_host,
        delay=args.delay,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
        max_retries=args.retries,
        user_agent=args.user_agent,
        allowed_domains=tuple(args.domains) if args.domains else None,
        blocked_domains=tuple(args.blocked_domains) if args.blocked_domains else None,
        include_patterns=tuple(args.include_patterns) if args.include_patterns else None,
        exclude_patterns=tuple(args.exclude_patterns) if args.exclude_patterns else None,
        respect_robots=False if args.ignore_robots else None,
        follow_external=True if args.follow_external else None,
        follow_nofollow=True if args.follow_nofollow else None,
        save_html=True if args.save_html else None,
        output_dir=args.output,
        verbose=False if args.quiet else None,
        use_sitemap=False if (args.no_sitemap or args.wizard) else None,
        dedupe_content=False if args.keep_duplicates else None,
        strip_params=False if args.keep_params else None,
        state_file="" if (args.no_resume or args.wizard) else (
            args.state_file or _default_state_file(args.output)),
        max_sitemaps=args.max_sitemaps,
        interaction_mode=args.interaction_mode,
        max_questions=args.max_questions,
        auto_actions=False if args.no_auto_actions else None,
        max_actions_per_page=args.max_actions_per_page,
        decisions_file=args.decisions_file or _default_decisions_file(args.output),
        login_url=args.login_url,
        login_username=args.username,
        login_password=args.password,
        login_password_env=args.password_env,
        auth_header=args.auth_header,
        session_file=(args.session_file or _default_session_file(args.output))
        if (args.session_file or args.login_url) else None,
        require_login=True if args.require_login else None,
        render=True if args.render else None,
        render_wait_until=args.render_wait_until,
        render_settle_ms=args.render_settle,
        render_timeout=args.render_timeout,
        render_cache=args.render_cache,
        no_render_cache=True if args.no_render_cache else None,
        extract_routes=False if args.no_extract_routes else None,
        include_dynamic_routes=True if args.include_dynamic_routes else None,
        max_scripts=args.max_scripts,
    )


def _default_decisions_file(output_dir: str) -> str:
    return os.path.join(output_dir, "decisions.json")


def _default_session_file(output_dir: str) -> str:
    return os.path.join(output_dir, "session.json")


def _default_state_file(output_dir: str) -> str:
    return os.path.join(output_dir, "crawl-state.json")


def print_summary(result: dict) -> None:
    summary = result["summary"]
    lines = [
        "",
        "=" * 58,
        "  crawl finished in %0.2fs" % summary["elapsed"],
        "=" * 58,
        "  pages crawled   : %d" % summary["pages_crawled"],
        "  successful      : %d" % summary["successful"],
        "  failed          : %d" % summary["failed"],
        "  duplicates      : %d" % summary.get("duplicates", 0),
        "  non-html skipped: %d" % summary["skipped"],
        "  robots blocked  : %d" % summary["robots_blocked"],
        "  unique hosts    : %d" % summary["unique_hosts"],
        "  total words     : %d" % summary["total_words"],
        "  emails found    : %d" % len(summary["all_emails"]),
    ]
    coverage = summary.get("coverage")
    if coverage:
        lines.append("  site coverage   : %s%% (%d/%d sitemap URLs)"
                     % (coverage.get("percent"), coverage.get("crawled"),
                        coverage.get("known_urls")))
    if summary.get("pending"):
        lines.append("  still queued    : %d (rerun with the same state file to continue)"
                     % summary["pending"])
    if summary.get("js_rendered_pages"):
        lines.append("  js-rendered pages: %d (content may be incomplete)"
                     % summary["js_rendered_pages"])
    site = summary.get("site") or {}
    if site.get("sitemap_sources"):
        lines.append("  sitemaps        : %s" % ", ".join(site["sitemap_sources"][:3]))

    render = summary.get("render") or {}
    if render.get("enabled"):
        lines.append("  rendered        : %d page(s), %d cache hit(s), %d failure(s)"
                     % (render.get("rendered", 0), render.get("cache_hits", 0),
                        render.get("failures", 0)))
    routes = summary.get("routes")
    if routes and routes.get("static_routes"):
        lines.append("  js routes       : %d static, %d dynamic (from %d script(s))"
                     % (routes["static_routes"], routes["dynamic_routes"],
                        routes["scripts_scanned"]))
        if routes["dynamic_routes"]:
            lines.append("  dynamic routes  : %s (need real values)"
                         % ", ".join(d["path"] for d in routes["dynamic"]))

    agent_info = summary.get("agent") or {}
    if agent_info.get("actions_total"):
        lines.append("  agent actions   : %d (%s)"
                     % (agent_info["actions_total"],
                        ", ".join("%s=%d" % kv for kv in
                                  sorted(agent_info["actions_by_kind"].items()))))
    interactions = agent_info.get("interactions") or {}
    if interactions:
        lines.append("  interactions    : mode=%s asked=%d skipped=%d"
                     % (interactions.get("mode"), interactions.get("questions_asked", 0),
                        interactions.get("questions_skipped", 0)))
    if summary.get("login_walls"):
        lines.append("  login-walled    : %d page(s) skipped"
                     % len(summary["login_walls"]))

    auth = summary.get("auth") or {}
    if auth.get("attempted"):
        lines.append("  login          : %s via %s"
                     % ("ok" if auth.get("ok") else "FAILED", auth.get("method")))
        if auth.get("reason"):
            lines.append("  login reason   : %s" % auth["reason"])
    elif auth.get("login_configured") or auth.get("auth_header"):
        lines.append("  login          : not attempted")
    problems = [p for p in result["pages"] if p.get("error")][:10]
    if problems:
        lines.append("")
        lines.append("  first errors:")
        for page in problems:
            lines.append("    - %s (%s)" % (page["url"], page["error"]))
    for label, path in (result.get("reports") or {}).items():
        lines.append("  %-5s -> %s" % (label, path))
    lines.append("=" * 58)
    print("\n".join(lines))


def print_plan(plan: dict) -> None:
    probe = plan["probe"]
    print("")
    print("=" * 58)
    print("  crawl plan")
    print("=" * 58)
    print("  target        : %s" % probe["base_url"])
    print("  host          : %s" % probe["host"])
    print("  robots.txt    : %s" % ("found" if probe["robots_exists"] else "not found"))
    if probe["sitemap_sources"]:
        print("  sitemaps      :")
        for source in probe["sitemap_sources"]:
            print("                  %s" % source)
    else:
        print("  sitemaps      : none found, will use link discovery")
    print("  urls found    : %d" % probe["urls_usable"])
    print("  estimated size: %d page(s)" % plan["estimate"]["est_pages"])
    print("  suggested     : depth %s, max_pages %s, concurrency %s, delay %ss"
          % (plan["recommended"]["max_depth"], plan["recommended"]["max_pages"],
             plan["recommended"]["concurrency"], plan["recommended"]["delay"]))
    if plan["sample_urls"]:
        print("  sample urls   :")
        for url in plan["sample_urls"][:8]:
            print("                  %s" % url)
    for note in plan["notes"]:
        print("  note          : %s" % note)
    print("=" * 58)
    print("")
    print("run without --plan-only to start the crawl")


def run_wizard() -> int:
    from supercrawler import CrawlConfig, crawl
    from supercrawler.fetcher import Fetcher
    from supercrawler.report import write_reports

    def fetch_text(url: str):
        with Fetcher(CrawlConfig(delay=0.3)) as probe_fetcher:
            result = probe_fetcher.get(url)
        return result.text if result.ok else None

    try:
        built = build_plan(fetcher_text=fetch_text)
    except (ValueError, KeyboardInterrupt) as exc:
        print("\nsetup cancelled: %s" % exc, file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2

    config = built["config"]
    answer = input("\nstart the crawl? [Y/n]: ").strip().lower()
    if answer and answer not in ("y", "yes"):
        print("nothing was crawled")
        return 0

    try:
        result = crawl(config, logger=print)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    result["reports"] = write_reports(result["pages"], result["summary"],
                                      output_dir=config.output_dir)
    print_summary(result)
    return 0


def print_routes(report) -> None:
    data = report.as_dict()
    print("")
    print("=" * 58)
    print("  routes found in JavaScript")
    print("=" * 58)
    print("  scripts read    : %d" % data["scripts_scanned"])
    if data["scripts_failed"]:
        print("  scripts unreadable: %d" % data["scripts_failed"])
    print("  static routes   : %d" % data["static_routes"])
    for path in data["static"]:
        print("      %s" % path)
    print("  dynamic routes  : %d" % data["dynamic_routes"])
    for item in data["dynamic"]:
        print("      %s   (needs values for: %s)"
              % (item["path"], ", ".join(item["params"]) or "?"))
    for note in data["notes"]:
        print("  note            : %s" % note)
    if data["dynamic_routes"]:
        print("")
        print("  Dynamic routes need real values. Enumerate them from the site's")
        print("  own API, or add --include-dynamic-routes to queue the templates")
        print("  (they will render as their not-found page until you do).")
    print("=" * 58)
    print("")
    print("  add --render to crawl these with a real browser")


def print_diagnosis(result: dict) -> None:
    """Explain a small or empty crawl in terms of the rule that caused it."""
    diagnosis = (result.get("summary") or {}).get("diagnosis") or {}
    reasons = diagnosis.get("reasons") or []
    suggestions = diagnosis.get("suggestions") or []
    if not reasons and not suggestions:
        return

    print("")
    print("=" * 58)
    print("  why this crawl stopped where it did")
    print("=" * 58)
    print("  pages fetched   : %d" % diagnosis.get("pages_fetched", 0))
    print("  links found     : %d internal" % diagnosis.get("internal_links_found", 0))
    for item in reasons:
        print("  - %s (x%d): %s" % (item["reason"], item["count"],
                                   item["explanation"]))
    for tip in suggestions:
        print("\n  -> %s" % tip)
    print("=" * 58)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.wizard:
        return run_wizard()

    if args.check_auth_header is not None:
        from supercrawler.auth import describe_token, inspect_token
        print("\n" + "=" * 58)
        print("  auth header check")
        print("=" * 58)
        if not args.check_auth_header.strip():
            print("  header        : INVALID (no value given)")
        else:
            print(describe_token(args.check_auth_header))
        print("=" * 58)
        return 0 if inspect_token(args.check_auth_header)["valid_header"] else 1

    if not args.seeds:
        print("error: at least one URL is required (or use --wizard)", file=sys.stderr)
        return 2

    try:
        config = config_from_args(args)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if config.max_pages < 0:
        print("error: --max-pages cannot be negative (use 0 for unlimited)",
              file=sys.stderr)
        return 2
    if config.max_depth < -1:
        print("error: --max-depth must be -1 (unlimited) or >= 0",
              file=sys.stderr)
        return 2

    if args.routes_only:
        if not config.extract_routes:
            print("error: --routes-only needs route extraction; drop "
                  "--no-extract-routes", file=sys.stderr)
            return 2
        crawler = Crawler(config, logger=print)
        base = seeds_first(config.seeds)
        if not base:
            print("error: no usable seed URL", file=sys.stderr)
            return 2

        def fetch_text(url: str):
            with Fetcher(config, logger=print) as probe:
                result = probe.get(url)
            return result.text if result.ok else None

        report = discover_routes(fetch_text, base,
                                 max_scripts=config.max_scripts, logger=print)
        print_routes(report)
        return 0

    if args.plan_only:
        if not config.use_sitemap:
            print("error: --plan-only needs sitemap discovery; drop --no-sitemap",
                  file=sys.stderr)
            return 2
        try:
            plan = Crawler(config, logger=print).plan()
        except (ValueError, Exception) as exc:
            print("error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
            return 1
        print_plan(plan)
        return 0

    try:
        result = crawl(config, logger=print, output_dir=None)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        if config.state_file:
            print("progress saved to %s; rerun to resume" % config.state_file)
        return 130
    except Exception as exc:
        print("error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1

    if not args.no_reports:
        from supercrawler.report import write_reports
        result["reports"] = write_reports(
            result["pages"], result["summary"],
            output_dir=args.output,
            html_path=args.html_out,
            csv_path=args.csv_out,
        )

    print_summary(result)
    if args.explain or len(result["pages"]) <= 2:
        print_diagnosis(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())