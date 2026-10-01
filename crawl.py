#!/usr/bin/env python3
"""Command line interface for the SuperCrawler package."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

import os

from supercrawler import PRESETS, get_preset, crawl
from supercrawler.crawler import Crawler
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
    )


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.wizard:
        return run_wizard()

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())