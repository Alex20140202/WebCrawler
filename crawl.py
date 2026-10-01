#!/usr/bin/env python3
"""Command line interface for the SuperCrawler package."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from supercrawler import PRESETS, get_preset, crawl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl.py",
        description="Polite, concurrent, robots-aware web crawler with reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("seeds", nargs="+", help="start URLs (https://example.com)")
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
    return parser


def config_from_args(args: argparse.Namespace):
    config = get_preset(args.preset)
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
    )


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
        "  non-html skipped: %d" % summary["skipped"],
        "  robots blocked  : %d" % summary["robots_blocked"],
        "  unique hosts    : %d" % summary["unique_hosts"],
        "  total words     : %d" % summary["total_words"],
        "  emails found    : %d" % len(summary["all_emails"]),
    ]
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = config_from_args(args)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if config.max_pages <= 0 or config.max_depth < 0:
        print("error: --max-pages must be > 0 and --max-depth >= 0", file=sys.stderr)
        return 2

    output_dir = None if args.no_reports else args.output

    try:
        result = crawl(config, logger=print, output_dir=None)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
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