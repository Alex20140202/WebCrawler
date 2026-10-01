# SuperCrawler

Whole-site crawler that finds every page by itself, scans each page for more work
to do, asks you only when the choice is genuinely ambiguous, and writes JSON /
JSONL / CSV / HTML reports.

Pure Python 3, two dependencies (`requests`, `beautifulsoup4`).

## Quick start

```bash
pip install -r requirements.txt

# guided setup, then crawl
python3 crawl.py --wizard

# one-shot whole-site crawl
python3 crawl.py https://example.com --smart

# let it decide and act on its own
python3 crawl.py https://example.com --smart --auto
```

## Logging in

For sites you own or are authorized to crawl:

```bash
# password from an environment variable, session reused on later runs
export SUPERCRAWLER_PASSWORD='...'
python3 crawl.py https://intranet.example.com/dashboard \
  --login-url https://intranet.example.com/login -u alice --require-login

# a token header instead of a form (API docs, CI systems)
python3 crawl.py https://api.example.com \
  --auth-header 'Authorization: Bearer ghp_xxx'
```

Login happens once, before any content request, on the same session the crawl
then uses. The authenticator:

- finds the login form by looking for a password field, scoring candidates so a
  search box is never mistaken for it
- picks the username and password field names rather than hardcoding them
- replays the form's hidden inputs, so CSRF tokens (`csrfmiddlewaretoken`,
  `authenticity_token`, and friends) are carried over
- confirms the login actually worked by checking it is no longer on a login form
  and that no failure text came back, so it never proceeds silently as anonymous
- accepts `--session-file` to save cookies and skip the form on later runs

### Two-factor

If the site asks for a code, the crawler stops and asks you for it rather than
trying to get around it. With `--ask` it prompts; with `--auto` it reports
`needs_totp` and continues without a session. A captcha page is detected and
refused outright — it will not attempt to solve one.

### Credential handling

- the password is read from `--password-env` (default `SUPERCRAWLER_PASSWORD`),
  so it stays out of shell history and process listings
- `--password` exists but is discouraged; the two together are rejected as a
  likely mistake
- the password never appears in logs, reports, or `AuthError` messages; log lines
  are redacted on the way out as a second line of defence
- `session.json` is written with `0600` permissions and is gitignored
- `--require-login` aborts rather than crawling anonymously if login fails

### What login does not do

It does not guess or brute-force credentials, solve captchas, bypass 2FA, or use
any session other than the one the site issued in response to your own login.
Login-walled pages discovered mid-crawl are listed in the report and skipped.

## Three modes

| flag | behaviour |
|------|-----------|
| `--auto` (default) | acts on safe defaults, never prompts |
| `--ask` | asks when a choice is ambiguous, once per topic |
| `--never-ask` | never prompts, never expands anything |

`--ask` degrades to `--auto` automatically when stdin is not a terminal, so it
never hangs in CI. Answers are cached per topic and written to
`output/decisions.json`; re-running with that file present reuses them, so an
unattended repeat run behaves like the attended one.

## Scanning and acting

Blind link-following misses pages nothing links to. So after each page loads,
the crawler scans the DOM for the things a human would click, then decides what
to do:

| signal detected | what it can do |
|-----------------|----------------|
| pager element, `?page=N` links | infer the paging scheme and fetch more pages |
| "Next" / "More" / "下一页" links | follow them |
| `<link rel=alternate>` RSS/Atom | fetch the feed, which often lists orphaned pages |
| GET search form | run search terms you supply |
| password field or sign-in wording | records the wall, does **not** sign in |
| "Load more" with `data-url` | notes the JSON endpoint |
| cookie consent wall | flags it, since content may be hidden behind it |
| JS shell with no text | flags it, since it needs a real browser |

Pagination is inferred from the observed link, so `?page=2`, `?start=10` and
`/page/2` all work, and the walk starts at the observed page so it never
re-fetches page 1. Pager links are tried in order until one actually reveals a
pattern, since the first one is usually "page 1".

**Every action is read-only.** It only ever queues URLs to fetch. It does not
submit POST forms and does not work around access controls. A GET search box is
the one form it will use, and only with terms you gave it. `--no-auto-actions`
turns the whole layer off.

Signing in is separate and happens once up front; see *Logging in* below. A page
the scanner spots as gated is reported and skipped, never used as a route into
the private area unless you logged in deliberately.

Caps you can set: `--max-actions-per-page` (default 25 URLs added per page),
`--max-questions` (default 10 per run).

## What "smart" means for discovery

1. reads `robots.txt` for `Sitemap:` declarations, then tries well-known sitemap
   paths, following sitemap *indexes* recursively
2. seeds the crawl with those URLs, then keeps link discovery running for
   anything the sitemap missed
3. sizes the run from what it found and suggests depth, budget and speed
4. skips assets and admin paths by default, strips `utm_*` tracking noise
5. collapses pages with identical content instead of storing them twice
6. reports coverage: how much of the sitemap it actually reached

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

# ask me when it finds a listing, cap follow-ups
python3 crawl.py https://example.com --ask --max-questions 5 --max-actions-per-page 10
```

Useful flags: `--max-pages`, `--max-depth`, `--concurrency`, `--per-host`,
`--delay`, `-d/--domain`, `--include`/`--exclude` (regex), `--ignore-robots`,
`--follow-external`, `--save-html`, `-o/--output`, `--state-file`,
`--no-resume`, `--keep-duplicates`, `--keep-params`, `--no-sitemap`.

### Presets

| preset      | depth    | pages    | concurrency | delay |
|-------------|----------|----------|-------------|-------|
| `fast`      | 2        | 100      | 16          | 0.1s  |
| `balanced`  | 3        | 500      | 8           | 0.5s  |
| `thorough`  | 6        | 5000     | 8           | 1.0s  |
| `polite`    | 3        | 300      | 2           | 2.0s  |
| `wholesite` | unlimited| unlimited| 8           | 0.8s  |
| `archive`   | unlimited| unlimited| 10          | 0.5s  |

`--smart` with the default preset switches you to `wholesite`.

## Resuming a long crawl

State is saved to `output/crawl-state.json` by default, so an interrupted or
budget-capped run continues where it stopped instead of starting over. Coverage
stays cumulative across runs. `--no-resume` starts fresh; `--state-file PATH`
relocates it. The state file is validated against the seed host, so pointing it
at a different site is safely ignored.

## Output

Written to `-o/--output` (default `output/`):

- `report.json` — summary, coverage, agent actions, and full page records
- `pages.jsonl` — one JSON page per line, for streaming into other tools
- `report.csv` — flat table, one row per page
- `report.html` — self-contained dark report: stats, coverage bar, host and depth
  breakdowns, crawler actions, login-walled pages, emails, page table
- `crawl-state.json` — resume state
- `decisions.json` — every question asked and what was decided
- `session.json` — saved cookies (gitignored, mode 0600) when you log in

Per page: status, content type, title and length, meta description, keywords,
canonical, lang, headings, word count, text preview, top terms, link counts,
image count, missing-alt count, viewport/OG/Twitter-card flags, emails, timings,
plus `findings` (what the scanner noticed) and `needs_login`.

## Library use

```python
from supercrawler import CrawlConfig, crawl

config = CrawlConfig(seeds=["https://example.com"], max_pages=0, max_depth=-1)
result = crawl(config, logger=print)
print(result["summary"]["coverage"])
print(result["summary"]["agent"]["actions_by_kind"])
```

Inspect before crawling:

```python
from supercrawler import Crawler, CrawlConfig

crawler = Crawler(CrawlConfig(seeds=["https://example.com"]))
plan = crawler.plan()
print(plan["probe"]["urls_usable"], plan["recommended"])
```

Drive the scanner and agent directly:

```python
from supercrawler import CrawlConfig, Interactor, scan_page
from supercrawler.agent import Agent

config = CrawlConfig(seeds=["https://example.com"])
agent = Agent(config, interactor=Interactor(mode="never"))
signals = scan_page(html, "https://example.com/blog", word_count=400, link_count=20)
extra_urls = agent.consider("https://example.com/blog", signals, depth=1)
```

Supply your own prompter to make the questions yours:

```python
Interactor(mode="ask", interactive=True, prompter=my_question_handler)
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
- every agent action is read-only, so it adds load no worse than ordinary
  link-following

`--ignore-robots` exists for sites you own or have written permission to crawl.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 130 tests, local fixture server
python3 tests/smoke_cli.py                 # end-to-end CLI check
```

Both run against a throwaway `http.server` fixture on localhost, so they never
touch the network. The fixture serves a sitemap index, a page sitemap, an orphan
page reachable only via sitemap, a robots-blocked path, a non-HTML file, a
JavaScript shell, a paginated blog, a login wall with a secret page behind it,
pages at several depths, and a login area with a CSRF token, a wrong-password
error path, a captcha variant, and a two-factor step.

## Layout

```
crawl.py                  CLI entry point
supercrawler/config.py    CrawlConfig dataclass and presets
supercrawler/discovery.py sitemap probing, URL policy, size estimation
supercrawler/scanner.py   DOM scanning: pagers, feeds, forms, walls
supercrawler/auth.py      login form handling, CSRF, 2FA, session reuse
supercrawler/agent.py     turns signals into queued URLs, read-only
supercrawler/interact.py  when to ask, when to decide alone
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
- Login covers ordinary HTML forms with a password field. Sites that authenticate
  through a JS-driven flow or an OAuth redirect need the cookie/header path
  (`--session-file` or `--auth-header`) instead.
- It will not solve a captcha, and it will not bypass 2FA; both are reported.
- "Load more" endpoints are reported but not fetched; they need a JSON parser.
- No sitemap and no links means no pages; that is inherent, not a bug.
- `robots.txt` is parsed by the standard library, which does not implement every
  wildcard edge case.
- Sitemaps over 100k entries are truncated, and the report says so.