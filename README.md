# SuperCrawler

Concurrent, rate-limited, robots-aware web crawler that extracts SEO and content
signals and writes JSON / JSONL / CSV / HTML reports.

Pure Python 3, two dependencies (`requests`, `beautifulsoup4`).

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# quick look at a site
python3 crawl.py https://example.com --preset fast

# deeper crawl, own output dir
python3 crawl.py https://example.com --max-depth 4 --max-pages 300 -o output

# stay on one subdomain, skip file downloads
python3 crawl.py https://example.com -d example.com --exclude '\.(pdf|zip|mp4)$'

# follow external links too
python3 crawl.py https://example.com --follow-external --max-pages 1000
```

`python3 crawl.py --help` lists every flag.

### Presets

| preset     | depth | pages | concurrency | delay |
|------------|-------|-------|-------------|-------|
| `fast`     | 2     | 100   | 16          | 0.1s  |
| `balanced` | 3     | 500   | 8           | 0.5s  |
| `thorough` | 6     | 5000  | 8           | 1.0s  |
| `polite`   | 3     | 300   | 2           | 2.0s  |

Any preset value can be overridden by the matching flag.

## Output

Written to `-o/--output` (default `output/`):

- `report.json` — summary plus full page records
- `pages.jsonl` — one JSON page per line, for streaming into other tools
- `report.csv` — flat table, one row per page
- `report.html` — self-contained dark-themed report with stats, host and depth
  breakdowns, extracted emails, and a sortable-feeling page table

With `--save-html`, raw HTML is kept under `output/pages/<sha1>.html`.

Per page you get: status, content type, title and length, meta description,
keywords, canonical, lang, headings, word count, text preview, top terms,
internal/external/nofollow link counts, image count, missing-alt count,
viewport/OG/Twitter-card flags, emails, and fetch timing.

## Library use

```python
from supercrawler import CrawlConfig, crawl

config = CrawlConfig(
    seeds=["https://example.com"],
    max_depth=2,
    max_pages=50,
    delay=0.5,
)

result = crawl(config, logger=print)
print(result["summary"]["pages_crawled"])
for page in result["pages"]:
    print(page["url"], page["title"])
```

Or pass `output_dir=` to `crawl()` to write reports in one call.

## Politeness and scope

On by default, and worth keeping on:

- reads and obeys `robots.txt`, including `Crawl-delay`
- per-host concurrency cap and per-host delay, so one slow site cannot starve others
- identifies itself with a configurable `User-Agent`
- exponential backoff with jitter on retries, limited to transient statuses
- `rel=nofollow` links are recorded but not followed
- stays on the seed's registrable domain unless you pass `--follow-external`

`--ignore-robots` exists for sites you own or have permission to crawl.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 21 tests, local fixture server
python3 tests/smoke_cli.py                 # end-to-end CLI check
```

Tests run against a throwaway `http.server` fixture on localhost, so they never
touch the network.

## Layout

```
crawl.py                  CLI entry point
supercrawler/config.py    CrawlConfig dataclass and presets
supercrawler/fetcher.py   HTTP: throttling, robots, retries, byte limits
supercrawler/parser.py    URL normalization and HTML extraction
supercrawler/report.py    JSON / JSONL / CSV / HTML writers
supercrawler/crawler.py   BFS orchestrator, scope filters, dedup
tests/                    unit tests and CLI smoke test
```# WebCrawler
