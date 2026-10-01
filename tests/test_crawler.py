from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from supercrawler import CrawlConfig, crawl, normalize_url, registrable_domain
from supercrawler.report import write_reports

INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>  Home &amp; Index  </title>
<meta name="description" content="Fixture home page">
<meta property="og:title" content="Home">
<meta name="viewport" content="width=device-width">
<meta name="keywords" content="fixture, testing, crawler">
<link rel="canonical" href="/index.html">
</head>
<body>
<h1>Home</h1>
<p>Contact us at hello@example.com for details about crawling testing fixtures.</p>
<a href="/about.html">About</a>
<a href="/deep/page.html">Deep</a>
<a href="http://external.test/elsewhere">External</a>
<a href="/private.html" rel="nofollow">NoFollow</a>
<a href="mailto:hello@example.com">Mail</a>
<a href="javascript:void(0)">JS</a>
<a href="/dup.html">Dup</a>
<a href="/dup.html#frag">Dup fragment</a>
<a href="//localhost:%(port)s/index.html">Protocol relative</a>
<img src="/logo.png" alt="Logo">
<img src="/bare.png">
</body>
</html>
"""

ABOUT = """<html><head><title>About</title></head><body>
<h1>About</h1><p>Short page.</p><a href="/">home</a></body></html>
"""

DEEP = """<html><head><title>Deep page</title>
<meta name="description" content="Level two"></head><body>
<h1>Deep</h1><p>Reached at depth two through about.</p>
<a href="/deeper.html">Deeper</a></body></html>
"""

DEEPER = """<html><head><title>Deeper page</title></head><body>
<h1>Deeper</h1><p>Content at depth two.</p>
<a href="/deepest.html">Deepest</a></body></html>
"""

DEEPEST = """<html><head><title>Deepest page</title></head><body>
<h1>Deepest</h1><p>At depth three, beyond max_depth of two.</p></body></html>
"""

DUP = "<html><head><title>Dup</title></head><body><p>dup target</p></body></html>"
PRIVATE = "<html><head><title>Private</title></head><body><p>should not be fetched</p></body></html>"
NOTFOUND = "<html><body>gone</body></html>"

ROBOTS = """User-agent: *
Disallow: /private.html
Crawl-delay: 0
Sitemap: %s/sitemap.xml
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/sitemap-pages.xml</loc></sitemap>
</sitemapindex>
"""

SITEMAP_PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/about.html</loc><lastmod>2024-01-02</lastmod></url>
  <url><loc>{base}/deep/page.html</loc></url>
  <url><loc>{base}/dup.html</loc></url>
  <url><loc>{base}/orphan.html</loc></url>
  <url><loc>{base}/notes.txt</loc></url>
  <url><loc>{base}/private.html</loc></url>
  <url><loc>http://external.test/offsite</loc></url>
</urlset>
"""

ORPHAN = """<html><head><title>Orphan</title></head><body>
<h1>Orphan</h1><p>Only reachable via sitemap, never linked from anywhere on the site.</p>
</body></html>"""

BLOG = """<html><head><title>Blog</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>
<body><h1>Blog</h1>
<div class="pagination">
  <a href="/blog.html">1</a><a href="/blog.html?page=2">2</a>
</div>
<a href="/blog.html?page=2">Next</a>
</body></html>"""

BLOG_PAGE = """<html><head><title>Blog page %s</title></head><body>
<h1>Post %s</h1><p>Distinct paginated article body number %s with enough words to
avoid being collapsed as a duplicate of another page in this fixture set.</p>
<a href="/blog.html">back</a></body></html>"""

MEMBERS = """<html><head><title>Members</title></head><body>
<h1>Members</h1>
<form action="/login" method="post">
<input type="text" name="user"><input type="password" name="pass">
<input type="submit" value="Sign in"></form>
<p>Please sign in to see this.</p>
<a href="/members-secret.html">Secret area</a></body></html>"""

MEMBERS_SECRET = """<html><head><title>Secret</title></head><body>
<h1>Secret</h1><p>Only reachable by following a link out of the login wall.</p>
</body></html>"""

# --- authenticated area fixtures -------------------------------------------

LOGIN_PAGE = """<html><head><title>Sign in</title></head><body>
<h1>Sign in</h1>
%(error)s
<form action="%(action)s" method="post" class="login-form">
  <input type="hidden" name="csrfmiddlewaretoken" value="tok-abc-123">
  <input type="hidden" name="next" value="/dashboard">
  <input type="text" name="username">
  <input type="password" name="password">
  <input type="submit" value="Sign in">
</form>
</body></html>"""

TOTP_PAGE = """<html><head><title>Two-factor</title></head><body>
<h1>Two-factor</h1>
<form action="/2fa" method="post">
  <input type="hidden" name="csrfmiddlewaretoken" value="tok-2fa">
  <input type="text" name="otp" placeholder="Verification code">
  <input type="submit" value="Verify">
</form>
</body></html>"""

CAPTCHA_PAGE = """<html><head><title>Sign in</title></head><body>
<form action="/login" method="post">
  <input type="text" name="username"><input type="password" name="password">
  <div class="g-recaptcha" data-sitekey="abc"></div>
</form>
</body></html>"""

DASHBOARD = """<html><head><title>Dashboard</title></head><body>
<h1>Dashboard</h1><p>Private overview with enough words to survive content dedupe.</p>
<a href="/reports.html">Reports</a></body></html>"""

REPORTS = """<html><head><title>Reports</title></head><body>
<h1>Reports</h1><p>Private report listing, distinct from the dashboard copy above.</p>
</body></html>"""

OK_USER = "scout"
OK_PASS = "correct-horse"
TOTP_CODE = "424242"

SPA_PAGE = """<html><head><title>SPA shell</title></head><body>
<div id="root"></div>
<script src="/app.js"></script><script src="/vendor.js"></script>
<script>window.__DATA__={}</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    port = 0
    session = set()

    def log_message(self, *args):
        pass

    def _session_ok(self) -> bool:
        raw = self.headers.get("Cookie") or ""
        return any(part.strip().endswith("sid=%s" % self.server.session_id)
                   for part in raw.split(";"))

    def _respond(self, status, body, content_type="text/html; charset=utf-8",
                 headers=None):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _read_post(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        pairs = []
        for chunk in body.split("&"):
            if not chunk:
                continue
            name, _, value = chunk.partition("=")
            pairs.append((name.replace("+", " "), value.replace("+", " ")))
        return dict(pairs)

    def do_POST(self):
        path = self.path.split("?")[0]
        form = self._read_post()

        if path == "/login" or path == "/login-totp":
            if form.get("username") == OK_USER and form.get("password") == OK_PASS:
                if form.get("csrfmiddlewaretoken") != "tok-abc-123":
                    self._respond(200, LOGIN_PAGE % {"error":
                                 "<p>CSRF token missing</p>",
                                 "action": self.path})
                    return
                target = "/2fa" if path == "/login-totp" else "/dashboard"
                self.send_response(302)
                self.send_header("Location", target)
                if path != "/login-totp":
                    self.send_header("Set-Cookie",
                                     "sid=%s; Path=/" % self.server.session_id)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._respond(200, LOGIN_PAGE % {
                "error": "<p>Invalid username or password.</p>",
                "action": self.path})
            return

        if path == "/2fa":
            if form.get("otp") == TOTP_CODE:
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.send_header("Set-Cookie", "sid=%s; Path=/" % self.server.session_id)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._respond(200, TOTP_PAGE)
            return

        self._respond(404, NOTFOUND)

    def do_GET(self):
        path = self.path.split("?")[0]
        base = "http://127.0.0.1:%d" % self.port
        if path == "/robots.txt":
            self._respond(200, ROBOTS % base, "text/plain")
        elif path == "/sitemap.xml":
            self._respond(200, SITEMAP_INDEX.format(base=base), "application/xml")
        elif path == "/sitemap-pages.xml":
            self._respond(200, SITEMAP_PAGES.format(base=base), "application/xml")
        elif path == "/orphan.html":
            self._respond(200, ORPHAN)
        elif path == "/blog.html":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            if "page=" in query:
                number = query.split("page=")[-1].split("&")[0]
                number = "".join(ch for ch in number if ch.isdigit()) or "2"
                self._respond(200, BLOG_PAGE % (number, number, number))
            else:
                self._respond(200, BLOG)
        elif path == "/members.html":
            self._respond(200, MEMBERS)
        elif path == "/members-secret.html":
            self._respond(200, MEMBERS_SECRET)
        elif path == "/login":
            self._respond(200, LOGIN_PAGE % {"error": "", "action": "/login"})
        elif path == "/login-totp":
            self._respond(200, LOGIN_PAGE % {"error": "", "action": "/login-totp"})
        elif path == "/captcha-login":
            self._respond(200, CAPTCHA_PAGE)
        elif path == "/2fa":
            self._respond(200, TOTP_PAGE)
        elif path in ("/dashboard", "/reports.html"):
            if not self._session_ok():
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._respond(200, DASHBOARD if path == "/dashboard" else REPORTS)
        elif path == "/spa.html":
            self._respond(200, SPA_PAGE)
        elif path in ("/", "/index.html"):
            self._respond(200, INDEX % {"port": self.port})
        elif path == "/about.html":
            self._respond(200, ABOUT)
        elif path == "/deep/page.html":
            self._respond(200, DEEP)
        elif path == "/deeper.html":
            self._respond(200, DEEPER)
        elif path == "/deepest.html":
            self._respond(200, DEEPEST)
        elif path == "/dup.html":
            self._respond(200, DUP)
        elif path == "/private.html":
            self._respond(200, PRIVATE)
        elif path == "/missing.html":
            self._respond(404, NOTFOUND)
        elif path == "/notes.txt":
            self._respond(200, "plain text", "text/plain")
        elif path == "/loop.html":
            self._respond(302, "", "text/html")
            self.send_header("Location", "/loop2.html")
            self.end_headers()
        else:
            self._respond(404, NOTFOUND)


class LocalSite:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.session_id = hashlib.sha1(
            str(self.server.server_address[1]).encode()
        ).hexdigest()[:12]
        self.port = self.server.server_address[1]
        Handler.port = self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port


class NormalizeUrlTests(unittest.TestCase):
    def test_strips_fragment_and_default_port(self):
        self.assertEqual(normalize_url("http://Example.COM:80/a#frag"), "http://example.com/a")

    def test_drops_non_http_schemes(self):
        for url in ("mailto:a@b.com", "javascript:void(0)", "tel:+123", "data:text/html,x", ""):
            self.assertIsNone(normalize_url(url), url)

    def test_resolves_relative_and_collapses_slashes(self):
        self.assertEqual(normalize_url("../b", "http://x.com/a/c/d"), "http://x.com/a/b")
        self.assertEqual(normalize_url("/a//b//", "http://x.com/"), "http://x.com/a/b")

    def test_keeps_query_and_non_default_port(self):
        self.assertEqual(normalize_url("http://x.com:8080/p?b=2&a=1"), "http://x.com:8080/p?b=2&a=1")

    def test_registrable_domain(self):
        self.assertEqual(registrable_domain("www.example.com"), "example.com")
        self.assertEqual(registrable_domain("a.b.example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("localhost"), "localhost")


class CrawlTests(unittest.TestCase):
    def test_crawls_within_scope_and_depth(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"],
                max_depth=2,
                max_pages=50,
                delay=0.0,
                concurrency=4,
                per_host_concurrency=4,
                use_sitemap=False,
            )
            result = crawl(config, logger=None)

        pages = {p["url"]: p for p in result["pages"]}
        summary = result["summary"]

        self.assertGreaterEqual(summary["pages_crawled"], 4)
        self.assertGreater(summary["successful"], 0)
        self.assertIn(site.base + "/about.html", pages)
        self.assertIn(site.base + "/deep/page.html", pages)
        self.assertIn(site.base + "/deeper.html", pages)
        self.assertNotIn(site.base + "/deepest.html", pages, "depth limit not enforced")
        self.assertEqual(pages[site.base + "/deeper.html"]["depth"], 2)
        self.assertNotIn(site.base + "/private.html", pages, "robots.txt not respected")
        self.assertNotIn("http://external.test/elsewhere", pages, "external link followed")

        home = pages[site.base + "/"]
        self.assertEqual(home["title"], "Home & Index")
        self.assertEqual(home["lang"], "en")
        self.assertEqual(home["meta_description"], "Fixture home page")
        self.assertTrue(home["has_og"])
        self.assertTrue(home["has_viewport"])
        self.assertEqual(home["h1_count"], 1)
        self.assertGreater(home["word_count"], 5)
        self.assertEqual(home["images_missing_alt"], 1)
        self.assertIn("hello@example.com", home["emails"])
        self.assertEqual(home["canonical"], site.base + "/index.html")

        targets = {u.split("/")[-1] for u in home["internal_links"]}
        self.assertIn("about.html", targets)
        self.assertIn("http://external.test/elsewhere", home["external_links"])
        self.assertNotIn("http://external.test/elsewhere", home["internal_links"])
        self.assertEqual(home["nofollow_links_count"], 1)

    def test_depth_limit_zero_fetches_only_seeds(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/"], max_depth=0, delay=0.0)
            result = crawl(config, logger=None)
        self.assertEqual(result["summary"]["pages_crawled"], 1)

    def test_max_pages_caps_results(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/"], max_depth=3, max_pages=2, delay=0.0)
            result = crawl(config, logger=None)
        self.assertLessEqual(result["summary"]["pages_crawled"], 2)

    def test_404_recorded_without_children(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/missing.html"], delay=0.0,
                                use_sitemap=False)
            result = crawl(config, logger=None)
        page = result["pages"][0]
        self.assertEqual(page["status"], 404)
        self.assertEqual(page["internal_links"], [])
        self.assertEqual(result["summary"]["failed"], 1)

    def test_non_html_is_recorded_but_not_parsed(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/notes.txt"], delay=0.0,
                                use_sitemap=False)
            result = crawl(config, logger=None)
        page = result["pages"][0]
        self.assertEqual(page["content_type"], "text/plain")
        self.assertEqual(page["skipped"], True)
        self.assertNotIn("title", page)
        self.assertEqual(result["summary"]["skipped"], 1)

    def test_include_pattern_filters_urls(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"],
                include_patterns=[r"about"],
                max_depth=2,
                delay=0.0,
            )
            result = crawl(config, logger=None)
        urls = [p["url"] for p in result["pages"]]
        self.assertEqual(set(urls), {site.base + "/", site.base + "/about.html"})

    def test_exclude_pattern_skips_discovered_links(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"],
                exclude_patterns=[r"/deep"],
                max_depth=3,
                delay=0.0,
            )
            result = crawl(config, logger=None)
        urls = [p["url"] for p in result["pages"]]
        self.assertIn(site.base + "/about.html", urls)
        self.assertNotIn(site.base + "/deep/page.html", urls)
        self.assertNotIn(site.base + "/deeper.html", urls)

    def test_seeds_bypass_include_filter(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/about.html"],
                include_patterns=[r"never-matches-anything"],
                delay=0.0,
            )
            result = crawl(config, logger=None)
        self.assertEqual([p["url"] for p in result["pages"]], [site.base + "/about.html"])

    def test_ignore_robots_allows_blocked_page(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/private.html"],
                respect_robots=False, delay=0.0,
            )
            result = crawl(config, logger=None)
        urls = [p["url"] for p in result["pages"]]
        self.assertIn(site.base + "/private.html", urls)

    def test_deduplicates_urls(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/"], max_depth=2, delay=0.0)
            result = crawl(config, logger=None)
        urls = [p["url"] for p in result["pages"]]
        self.assertEqual(len(urls), len(set(urls)))

    def test_invalid_seed_raises(self):
        config = CrawlConfig(seeds=["mailto:nope@example.com"])
        with self.assertRaises(ValueError):
            crawl(config, logger=None)

    def test_save_html_writes_files(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            config = CrawlConfig(
                seeds=[site.base + "/about.html"], delay=0.0,
                save_html=True, output_dir=tmp, use_sitemap=False,
            )
            result = crawl(config, logger=None)
            saved = result["pages"][0]["saved_html"]
            self.assertTrue(os.path.exists(saved))
            with open(saved, encoding="utf-8") as handle:
                self.assertIn("About", handle.read())


class ReportTests(unittest.TestCase):
    def test_reports_written_and_readable(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/"], max_depth=1, delay=0.0)
            result = crawl(config, logger=None)

        with tempfile.TemporaryDirectory() as tmp:
            written = write_reports(result["pages"], result["summary"], output_dir=tmp)
            self.assertEqual(set(written), {"json", "jsonl", "csv", "html"})
            for path in written.values():
                self.assertTrue(os.path.exists(path), path)
                self.assertGreater(os.path.getsize(path), 0, path)

            with open(written["json"], encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["summary"]["pages_crawled"], len(payload["pages"]))

            with open(written["csv"], encoding="utf-8") as handle:
                lines = handle.read().strip().splitlines()
            self.assertEqual(lines[0].split(",")[0], "url")
            self.assertEqual(len(lines) - 1, len(payload["pages"]))

            with open(written["jsonl"], encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(records), len(payload["pages"]))

            with open(written["html"], encoding="utf-8") as handle:
                document = handle.read()
            self.assertIn("<!DOCTYPE html>", document)
            self.assertIn("Pages crawled", document)
            self.assertIn("hello@example.com", document)


class ConfigTests(unittest.TestCase):
    def test_merged_rejects_unknown_option(self):
        with self.assertRaises(ValueError):
            CrawlConfig().merged(nonexistent=1)

    def test_merged_ignores_none(self):
        base = CrawlConfig()
        self.assertEqual(base.merged(max_depth=None), base)

    def test_merged_overrides(self):
        self.assertEqual(CrawlConfig().merged(max_depth=9).max_depth, 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)