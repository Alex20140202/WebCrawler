# SuperCrawler

Whole-site crawler that finds every page by itself, asks you only what it needs to
know, and writes JSON / JSONL / CSV / HTML reports.

Pure Python 3, two dependencies (`requests`, `beautifulsoup4`).

## Quick start

```bash
pip install -r requirements.txt

# guided: answer a few questions, it plans and crawls
python3 crawl.py --wizard

# or one-shot whole-site crawl
python3 crawl.py https://example.com --smart
```

## What "smart" means

Blind link-following misses pages nothing links to (old posts, orphans, paginated
listings) and wastes requests on assets. So in smart mode it:

1. reads `robots.txt` for `Sitemap:` declarations, then tries the well-known
   sitemap paths, following sitemap *indexes* recursively
2. seeds the crawl with those URLs, then keeps link discovery running for
   anything the sitemap missed
3. sizes the run from what it found and suggests depth, budget and speed
4. skips assets and admin paths by default, and strips `utm_*` tracking noise
5. collapses pages with identical content instead of storing them twice
6. flags pages that look JavaScript-rendered so you know the text is thin
7. reports coverage: how much of the sitemap it actually reached

If there is no sitemap it falls back to link discovery and says so.

## Options the wizard asks about

| question | why it matters |
|----------|----------------|
| site URL | the only truly required answer; a bare domain gets `https://` |
| scope | whole site, a path prefix, one page, or your own URL list |
| goal | content, SEO audit, link graph, or contacts; sets sensible URL filters |
| page budget | capped or unlimited; unlimited stops when links run out |
| link depth | `-1` unlimited, `0` seeds only |
| speed | polite / balanced / fast, then delay and concurrency |
| extras | robots.txt, sitemap use, dedupe, raw HTML saving, external links |

Every answer has a default, so pressing Enter accepts it. It prints the finished
plan and asks for confirmation before touching the network.

## CLI

```bash
# see the plan without crawling
python3 crawl.py https://example.com --smart --plan-only

# whole site, unlimited, resumable
python3 crawl.py https://example.com --smart --preset wholesite

# inspect a plan's size first
python3 crawl.py https://example.com --smart --plan-only | head -20
```

Useful flags: `--max-pages`, `--max-depth`, `--concurrency`, `--per-host`,
`--delay`, `-d/--domain`, `--include`/`--exclude` (regex), `--ignore-robots`,
`--follow-external`, `--save-html`, `-o/--output`, `--state-file`,
`--no-resume`, `--keep-duplicates`, `--keep-params`, `--no-sitemap`.

### Presets

| preset     | depth | pages    | concurrency | delay |
|------------|-------|----------|-------------|-------|
| `fast`     | 2     | 100      | 16          | 0.1s  |
| `balanced` | 3     | 500      | 8           | 0.5s  |
| `thorough` | 6     | 5000     | 8           | 1.0s  |
| `polite`   | 3     | 300      | 2           | 2.0s  |
| `wholesite`| 12    | unlimited| 8           | 0.8s  |
| `archive`  | 25    | unlimited| 10          | 0.5s  |

`--smart` with the default preset switches you to `wholesite`.

## Resuming a long crawl

State is saved to `output/crawl-state.json` by default, so an interrupted or
budget-capped run continues where it stopped instead of starting over. Coverage
stays cumulative across runs. `--no-resume` starts fresh; `--state-file PATH`
relocates it. The state file is validated against the seed host, so pointing it
at a different site is safely ignored.

## Output

Written to `-o/--output` (default `output/`):

- `report.json` — summary, coverage, and full page records
- `pages.jsonl` — one JSON page per line, for streaming into other tools
- `report.csv` — flat table, one row per page
- `report.html` — self-contained dark report: stats, coverage bar, host and depth
  breakdowns, extracted emails, page table
- `crawl-state.json` — resume state

Per page: status, content type, title and length, meta description, keywords,
canonical, lang, headings, word count, text preview, top terms, link counts,
image count, missing-alt count, viewport/OG/Twitter-card flags, emails, timings.

## Library use

```python
from supercrawler import CrawlConfig, crawl

config = CrawlConfig(seeds=["https://example.com"], max_pages=0, max_depth=-1)
result = crawl(config, logger=print)
print(result["summary"]["coverage"])
```

Inspect before crawling:

```python
from supercrawler import Crawler, CrawlConfig

crawler = Crawler(CrawlConfig(seeds=["https://example.com"]))
plan = crawler.plan()
print(plan["probe"]["urls_usable"], plan["recommended"])
```

Sitemap work on its own:

```python
from supercrawler import parse_sitemap, candidate_sitemaps

entries, nested = parse_sitemap(xml_text)
```

## Politeness and scope

On by default, and worth keeping on:

- reads and obeys `robots.txt`, including `Crawl-delay`; blocked URLs are never
  fetched and are counted separately
- per-host concurrency cap and per-host delay, so one slow site cannot starve others
- identifies itself with a configurable `User-Agent`
- exponential backoff with jitter, limited to transient statuses
- `rel=nofollow` links are recorded but not followed
- stays on the seed's registrable domain unless you pass `--follow-external`
- stops early if the recent error rate goes above `stop_on_error_ratio`

`--ignore-robots` exists for sites you own or have written permission to crawl.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 62 tests, local fixture server
python3 tests/smoke_cli.py                 # end-to-end CLI check
```

Both run against a throwaway `http.server` fixture on localhost, so they never
touch the network. The fixture serves a sitemap index, a page sitemap, an orphan
page reachable only via sitemap, a robots-blocked path, a non-HTML file, a
JavaScript shell, and pages at several depths.

## Layout

```
crawl.py                  CLI entry point
supercrawler/config.py    CrawlConfig dataclass and presets
supercrawler/discovery.py sitemap probing, URL policy, size estimation
supercrawler/fetcher.py   HTTP: throttling, robots, retries, byte limits
supercrawler/parser.py    URL normalization and HTML extraction
supercrawler/state.py     resumable crawl state
supercrawler/wizard.py    interactive Q&A
supercrawler/report.py    JSON / JSONL / CSV / HTML writers
supercrawler/crawler.py   BFS orchestrator, scope filters, coverage, dedupe
tests/                    unit tests and CLI smoke test
```

## Limits

- `html.parser` only, so it reads server-rendered HTML. JavaScript-rendered pages
  are detected and flagged, not rendered. Adding `playwright` would fix that.
- No sitemap and no links means no pages; that is inherent, not a bug.
- `robots.txt` is parsed by the standard library, which does not implement every
  wildcard edge case.
- Sitemaps over 100k entries are truncated, and the report says so.