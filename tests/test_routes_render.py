from __future__ import annotations

import json
import os
import tempfile
import unittest

from supercrawler import CrawlConfig, crawl
from supercrawler.parser import extract_links, normalize_url
from supercrawler.render import (
    RenderCache,
    RenderError,
    RenderResult,
    Renderer,
    playwright_available,
)
from supercrawler.routes import (
    collect_imports,
    collect_js_urls,
    discover_routes,
    extract_routes_from_js,
    hash_variants,
    is_dynamic,
    looks_like_route,
    param_names,
    route_urls,
)

from test_crawler import LocalSite

SPIN = """// route table
const NAV = [
  { path: '/', label: 'Home' },
  { path: '/blog', label: 'Blog' },
  { path: '/notes', label: 'Notes' },
];
for (const item of NAV) route(item.path, view(item));
route('/dashboard', view(dashboard), { auth: true });
route('/blog/:slug', view(post));
route('/u/:username', view(userProfile));
route('/blog/:slug/edit', view(editor));
const go = (p) => location.hash = '#' + p;
"""

REACT_ROUTER = """import { Routes, Route } from 'react-router-dom';
const routes = [
  <Route path="/about" element={<About/>} />,
  <Route path="/pricing" element={<Pricing/>} />,
];
export default function App(){ return <Routes>{routes}</Routes>; }
"""

VUE_ROUTER = """const routes = [
  { path: '/', component: Home },
  { path: '/users/:id', component: User },
  { path: '/files/*', component: Files },
];
"""

TOTP_ATTACHED = """const href = "#/inbox";
export function nav(){ return '<a href="#/inbox">Inbox</a>'; }
"""


class RouteExtractionTests(unittest.TestCase):
    def test_custom_router_registration(self):
        found = {c.path for c in extract_routes_from_js(SPIN, "/js/main.js")}
        for expected in ("/blog", "/dashboard", "/blog/:slug", "/u/:username"):
            self.assertIn(expected, found)

    def test_react_router_jsx(self):
        found = {c.path for c in extract_routes_from_js(REACT_ROUTER, "/a.js")}
        self.assertEqual(found, {"/about", "/pricing"})

    def test_vue_router_objects(self):
        found = {c.path for c in extract_routes_from_js(VUE_ROUTER, "/b.js")}
        self.assertEqual(found, {"/", "/users/:id", "/files/*"})

    def test_hash_literals_found(self):
        found = {c.path for c in extract_routes_from_js(TOTP_ATTACHED, "/c.js")}
        self.assertIn("/inbox", found)

    def test_dynamic_detection_and_params(self):
        self.assertTrue(is_dynamic("/blog/:slug"))
        self.assertTrue(is_dynamic("/files/*"))
        self.assertTrue(is_dynamic("/users/[id]"))
        self.assertTrue(is_dynamic("/x/<name>"))
        self.assertFalse(is_dynamic("/blog/2024"))
        self.assertEqual(sorted(param_names("/blog/:slug/edit")), ["slug"])

    def test_non_routes_rejected(self):
        for value in ("https://cdn.example/x.js", "data:text/html,x", "/a.pdf",
                      "app.js", "mailto:a@b.c", ""):
            self.assertFalse(looks_like_route(value), value)

    def test_javascript_bundles_and_imports(self):
        html = ('<script src="/js/main.js"></script>'
                '<link rel="modulepreload" href="/js/lib/router.js">'
                '<script type="module">import x from "/js/views/home.js";</script>')
        urls = collect_js_urls(html, "https://x.test/")
        self.assertIn("https://x.test/js/main.js", urls)
        self.assertIn("https://x.test/js/lib/router.js", urls)

        imports = collect_imports("import a from './views/home.js';"
                                  "import('./lazy.js');", "https://x.test/js/main.js")
        self.assertIn("https://x.test/js/views/home.js", imports)
        self.assertIn("https://x.test/js/lazy.js", imports)

    def test_route_urls_and_hash_variants(self):
        class FakeReport:
            base_url = "https://x.test/"
            static = [type("C", (), {"path": "/blog"})(),
                      type("C", (), {"path": "/notes"})()]
            dynamic = []

        urls = route_urls(FakeReport())
        self.assertEqual(urls, ["https://x.test/blog", "https://x.test/notes"])
        variants = hash_variants("https://x.test/", ["/blog", "/notes"])
        self.assertEqual(variants, ["https://x.test#/blog", "https://x.test#/notes"])

    def test_root_route_is_kept(self):
        found = {c.path for c in extract_routes_from_js(VUE_ROUTER, "/b.js")}
        self.assertIn("/", found, "the root route must not be dropped")


class RouteDiscoveryTests(unittest.TestCase):
    def _fetcher(self, pages):
        def fetch(url):
            return pages.get(url)
        return fetch

    def test_discovers_routes_end_to_end(self):
        pages = {
            "https://x.test/": '<script src="/js/main.js"></script>',
            "https://x.test/js/main.js": SPIN,
        }
        report = discover_routes(self._fetcher(pages), "https://x.test/")
        static = {c.path for c in report.static}
        self.assertIn("/blog", static)
        self.assertIn("/dashboard", static)
        self.assertEqual(len(report.dynamic), 3)
        self.assertEqual(report.scripts_scanned, ["/js/main.js"] if False
                         else report.scripts_scanned)
        self.assertTrue(report.notes)

    def test_follows_imports_one_level(self):
        pages = {
            "https://x.test/": '<script src="/js/main.js"></script>',
            "https://x.test/js/main.js": "import r from './lib/routes.js';",
            "https://x.test/js/lib/routes.js": SPIN,
        }
        report = discover_routes(self._fetcher(pages), "https://x.test/")
        self.assertIn("/blog", {c.path for c in report.static})

    def test_unreadable_script_is_recorded(self):
        pages = {
            "https://x.test/": '<script src="/js/main.js"></script>'
                               '<script src="/js/gone.js"></script>',
            "https://x.test/js/main.js": SPIN,
        }
        report = discover_routes(self._fetcher(pages), "https://x.test/")
        self.assertIn("https://x.test/js/gone.js", report.scripts_failed)

    def test_no_javascript_is_noted(self):
        report = discover_routes(self._fetcher({"https://x.test/": "<html></html>"}),
                                 "https://x.test/")
        self.assertEqual(report.total, 0)
        self.assertTrue(any("no JavaScript" in n for n in report.notes))

    def test_unfetchable_page_reported(self):
        report = discover_routes(self._fetcher({}), "https://x.test/")
        self.assertIn("could not fetch", report.notes[0])


class FragmentLinkTests(unittest.TestCase):
    HTML = ('<html><body><nav>'
            '<a href="#/blog">Blog</a><a href="#/notes">Notes</a>'
            '<a href="#/">Home</a><a href="/about">About</a>'
            '</nav></body></html>')

    def test_hash_routes_kept_separate(self):
        links = extract_links(self.HTML, "https://x.test/")
        self.assertEqual(sorted(links["fragments"]), ["/blog", "/notes"])
        self.assertEqual(links["internal"], ["https://x.test/about"])

    def test_bare_hash_is_not_a_route(self):
        links = extract_links('<a href="#/">Home</a>', "https://x.test/")
        self.assertEqual(links["fragments"], [])

    def test_anchor_hash_ignored(self):
        links = extract_links('<a href="#section">Jump</a>', "https://x.test/")
        self.assertEqual(links["fragments"], [])


class ShellDetectionTests(unittest.TestCase):
    def test_visible_length_ignores_markup(self):
        shell = ("<html><head><title>T</title></head><body>"
                 "<div id='root'></div><script>var a=1;</script></body></html>")
        from supercrawler.fetcher import _needs_render, visible_text_length
        self.assertLess(visible_text_length(shell), 60)

    def test_spa_shell_needs_render(self):
        from supercrawler.fetcher import FetchResult, _needs_render
        shell = ("<html><head><title>T</title></head><body>"
                 "<div id='root'></div></body></html>" + "<!-- x -->" * 200)
        result = FetchResult("https://x.test/", status=200, text=shell,
                             content_type="text/html")
        self.assertTrue(_needs_render(result))

    def test_real_content_does_not_need_render(self):
        from supercrawler.fetcher import FetchResult, _needs_render
        rich = ("<html><body><article><h1>Title</h1><p>%s</p></article>"
                "</body></html>" % ("a sentence of prose " * 60))
        result = FetchResult("https://x.test/", status=200, text=rich,
                             content_type="text/html")
        self.assertFalse(_needs_render(result))

    def test_non_html_never_needs_render(self):
        from supercrawler.fetcher import FetchResult, _needs_render
        result = FetchResult("https://x.test/a.json", status=200, text="{}",
                             content_type="application/json")
        self.assertFalse(_needs_render(result))


class RenderCacheTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = RenderCache(tmp)
            cache.put(RenderResult("https://x.test/#/blog", html="<html>hi</html>",
                                   status=200, title="Blog"))
            self.assertEqual(len(cache), 1)
            got = cache.get("https://x.test/#/blog")
            self.assertIsNotNone(got)
            self.assertTrue(got.from_cache)
            self.assertEqual(got.html, "<html>hi</html>")
            self.assertEqual(got.title, "Blog")

    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            RenderCache(tmp).put(RenderResult("u1", html="<html>a</html>", status=200))
            self.assertIsNotNone(RenderCache(tmp).get("u1"))

    def test_failed_result_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = RenderCache(tmp)
            cache.put(RenderResult("u1", error="boom"))
            self.assertEqual(len(cache), 0)

    def test_missing_entry_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(RenderCache(tmp).get("never-rendered"))

    def test_corrupt_index_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as h:
                h.write("{not json")
            self.assertEqual(len(RenderCache(tmp)), 0)

    def test_clear_empties_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = RenderCache(tmp)
            cache.put(RenderResult("u1", html="<html>a</html>", status=200))
            cache.clear()
            self.assertEqual(len(cache), 0)
            self.assertIsNone(cache.get("u1"))

    def test_distinct_urls_get_distinct_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = RenderCache(tmp)
            cache.put(RenderResult("u1", html="<html>1</html>", status=200))
            cache.put(RenderResult("u2", html="<html>2</html>", status=200))
            self.assertNotEqual(cache.path_for("u1"), cache.path_for("u2"))


class RendererDegradationTests(unittest.TestCase):
    """The renderer must fail politely when playwright is absent."""

    def test_missing_playwright_yields_an_error_result(self):
        config = CrawlConfig(seeds=["https://x.test/"], render=True)
        renderer = Renderer(config)
        try:
            result = renderer.render("https://x.test/#/blog")
        finally:
            renderer.close()
        if playwright_available():
            self.assertTrue(result.ok or result.error)
        else:
            self.assertFalse(result.ok)
            self.assertIn("playwright", (result.error or "").lower())

    def test_cache_hit_needs_no_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = RenderCache(tmp)
            cache.put(RenderResult("https://x.test/#/blog", html="<html>c</html>",
                                   status=200))
            renderer = Renderer(CrawlConfig(render=True), cache=cache)
            try:
                result = renderer.render("https://x.test/#/blog")
            finally:
                renderer.close()
        self.assertTrue(result.ok)
        self.assertTrue(result.from_cache)
        self.assertEqual(renderer.cache_hits, 1)

    def test_summary_shape(self):
        renderer = Renderer(CrawlConfig(render=True))
        try:
            summary = renderer.summary()
        finally:
            renderer.close()
        for key in ("rendered", "cache_hits", "failures", "cache_entries"):
            self.assertIn(key, summary)

    def test_disabled_config(self):
        self.assertFalse(Renderer(CrawlConfig(render=False)).enabled)

    def test_context_manager_closes(self):
        with Renderer(CrawlConfig(render=True)) as renderer:
            self.assertIsNotNone(renderer)


class ConfigRenderTests(unittest.TestCase):
    def test_render_defaults_off_but_route_extraction_on(self):
        self.assertFalse(CrawlConfig().render,
                         "rendering needs an explicit opt-in")
        self.assertTrue(CrawlConfig().extract_routes,
                        "route discovery is harmless without a browser")

    def test_wait_until_is_validated(self):
        CrawlConfig(render_wait_until="networkidle").validate()
        with self.assertRaises(ValueError):
            CrawlConfig(render_wait_until="whenever").validate()

    def test_settle_and_timeout_bounds(self):
        CrawlConfig(render_settle_ms=0, render_timeout=1000).validate()
        with self.assertRaises(ValueError):
            CrawlConfig(render_settle_ms=-1).validate()
        with self.assertRaises(ValueError):
            CrawlConfig(render_timeout=0).validate()


class NoRenderCrawlTests(unittest.TestCase):
    def test_routes_ignored_when_render_is_off(self):
        """Route extraction must not silently queue unusable URLs."""
        with LocalSite() as site:
            result = crawl(CrawlConfig(seeds=[site.base + "/"], delay=0.0,
                                       use_sitemap=False, render=False,
                                       verbose=False), logger=None)
        self.assertIsNone(result["summary"]["routes"])
        self.assertFalse(result["summary"]["render"]["enabled"])
        self.assertTrue(all("#" not in p["url"] for p in result["pages"]))

    def test_render_flag_reported_when_off(self):
        with LocalSite() as site:
            result = crawl(CrawlConfig(seeds=[site.base + "/"], delay=0.0,
                                       use_sitemap=False, verbose=False),
                           logger=None)
        render = result["summary"]["render"]
        self.assertFalse(render["enabled"])
        self.assertIn("available", render)


if __name__ == "__main__":
    unittest.main(verbosity=2)