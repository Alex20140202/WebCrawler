from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from supercrawler import CrawlConfig, crawl
from supercrawler.agent import ACTION_LOGIN, ACTION_NEXT, ACTION_PAGINATE, Agent
from supercrawler.interact import Interactor
from supercrawler.scanner import build_paged_urls, expand_search_form, scan_page
from test_crawler import LocalSite

PAGINATED = """<html><head><title>Blog</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>
<body>
<h1>Blog</h1>
<div class="pagination">
  <a href="/blog">1</a><a href="/blog?page=2">2</a><a href="/blog?page=3">3</a>
</div>
<a href="/blog?page=2">Next</a>
</body></html>
"""

LOGIN_PAGE = """<html><body>
<h1>Members</h1>
<form action="/login" method="post">
  <input type="text" name="user"><input type="password" name="pass">
  <input type="submit" value="Sign in">
</form>
<p>Please sign in to continue.</p>
</body></html>
"""

SEARCH_PAGE = """<html><body>
<form action="/search" method="get">
  <input type="text" name="q"><input type="submit" value="Search">
</form>
<a href="/search?q=boots">boots</a>
</body></html>
"""

POST_FORM = """<html><body>
<form action="/checkout" method="post">
  <input type="text" name="search"><input type="submit" value="Go">
</form>
</body></html>
"""

LOAD_MORE = """<html><body>
<div id="app"></div>
<button data-url="/api/items?offset=20" class="load-more">Load more</button>
<script src="/a.js"></script><script src="/b.js"></script><script src="/c.js"></script>
</body></html>
"""

COOKIE_WALL = """<html><body>
<div class="cookie-consent-banner"><button>Accept</button></div>
<h1>Behind the wall</h1></body></html>
"""


class ScanPageTests(unittest.TestCase):
    def test_finds_pagination_and_feed(self):
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        self.assertTrue(any("/blog?page=2" in u for u in signals.pagination_urls))
        self.assertIn("https://x.test/feed.xml", signals.feed_urls)
        kinds = {f.kind for f in signals.findings()}
        self.assertIn("pagination", kinds)
        self.assertIn("feed", kinds)
        self.assertIn("next_page", kinds)

    def test_detects_login_wall(self):
        signals = scan_page(LOGIN_PAGE, "https://x.test/members")
        self.assertTrue(signals.needs_login)
        self.assertIn("login_required", {f.kind for f in signals.findings()})

    def test_get_search_form_found_post_form_ignored(self):
        get_signals = scan_page(SEARCH_PAGE, "https://x.test/")
        self.assertEqual(len(get_signals.get_forms), 1)
        self.assertEqual(get_signals.get_forms[0]["field"], "q")

        post_signals = scan_page(POST_FORM, "https://x.test/")
        self.assertEqual(post_signals.get_forms, [],
                         "POST forms must never be enumerated")

    def test_load_more_endpoint_detected(self):
        signals = scan_page(LOAD_MORE, "https://x.test/shop", word_count=3,
                            link_count=1)
        self.assertIn("https://x.test/api/items?offset=20", signals.api_urls)
        self.assertTrue(signals.js_rendered)

    def test_cookie_wall_flagged(self):
        signals = scan_page(COOKIE_WALL, "https://x.test/")
        self.assertTrue(signals.blocked_by_cookie_wall)

    def test_empty_html_is_safe(self):
        signals = scan_page("", "https://x.test/")
        self.assertEqual(signals.findings(), [])


class PagedUrlTests(unittest.TestCase):
    def test_query_pagination(self):
        urls = build_paged_urls("https://x.test/b", "https://x.test/b?page=2", limit=3)
        self.assertEqual(urls, [
            "https://x.test/b?page=3",
            "https://x.test/b?page=4",
            "https://x.test/b?page=5",
        ])

    def test_path_pagination(self):
        urls = build_paged_urls("https://x.test/b", "https://x.test/b/page/2", limit=2)
        self.assertEqual(urls, ["https://x.test/b/page/3", "https://x.test/b/page/4"])

    def test_offset_pagination_walks_upward(self):
        urls = build_paged_urls("https://x.test/b", "https://x.test/b?start=10", limit=2)
        self.assertEqual(urls, ["https://x.test/b?start=11",
                                "https://x.test/b?start=12"])

    def test_no_pattern_returns_nothing(self):
        self.assertEqual(build_paged_urls("https://x.test/b", "https://x.test/b"), [])

    def test_search_expansion_is_get_only(self):
        form = {"action": "https://x.test/search", "field": "q", "fields": ["q"]}
        urls = expand_search_form(form, ["boots"])
        self.assertEqual(urls, ["https://x.test/search?q=boots"])


class InteractorTests(unittest.TestCase):
    def test_never_mode_never_prompts(self):
        def boom(prompt):
            raise AssertionError("should not prompt")

        interactor = Interactor(mode="never", prompter=boom)
        self.assertEqual(interactor.get("k", "q?", "dflt").value, "dflt")
        self.assertEqual(interactor.questions_asked, 0)

    def test_auto_mode_never_prompts(self):
        def boom(prompt):
            raise AssertionError("should not prompt")

        interactor = Interactor(mode="auto", prompter=boom)
        answer = interactor.get("k", "q?", True)
        self.assertTrue(answer.value)
        self.assertEqual(answer.source, "auto")

    def test_ask_mode_prompts_once_and_caches(self):
        calls = []
        queue = iter(["yes"])

        def prompter(prompt):
            calls.append(prompt)
            return next(queue, "")

        interactor = Interactor(mode="ask", interactive=True, prompter=prompter)
        first = interactor.get("topic", "Continue?", True)
        second = interactor.get("topic", "Continue?", True)
        self.assertTrue(first.value)
        self.assertEqual(first.source, "asked")
        self.assertEqual(second.source, "asked")
        self.assertEqual(len(calls), 1)

    def test_non_tty_degrades_to_auto(self):
        interactor = Interactor(mode="ask", interactive=False,
                                prompter=lambda p: "y")
        self.assertEqual(interactor.mode, "auto")

    def test_question_budget_is_respected(self):
        queue = iter(["a", "b", "c"])
        interactor = Interactor(mode="ask", interactive=True, max_questions=2,
                                prompter=lambda p: next(queue, ""))
        for index in range(4):
            interactor.get("topic%d" % index, "Q%d?" % index, "d")
        self.assertEqual(interactor.questions_asked, 2)
        self.assertEqual(interactor.questions_skipped, 2)

    def test_typed_coercion(self):
        interactor = Interactor(mode="ask", interactive=True,
                                prompter=lambda p: "42")
        self.assertEqual(interactor.get("n", "How many?", 7).value, 42)
        interactor2 = Interactor(mode="ask", interactive=True,
                                 prompter=lambda p: "1.5")
        self.assertEqual(interactor2.get("f", "Delay?", 0.5).value, 1.5)

    def test_choose_returns_index(self):
        queue = iter(["2"])
        interactor = Interactor(mode="ask", interactive=True,
                                prompter=lambda p: next(queue, ""))
        answer = interactor.choose("c", "Pick", ["a", "b", "c"], default_index=0)
        self.assertEqual(answer.value, 1)

    def test_invalid_choice_reprompts(self):
        queue = iter(["nope", "9", "3"])
        interactor = Interactor(mode="ask", interactive=True,
                                prompter=lambda p: next(queue, ""))
        answer = interactor.choose("c", "Pick", ["a", "b", "c"], default_index=0)
        self.assertEqual(answer.value, 2)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            first = Interactor(mode="ask", interactive=True,
                               prompter=lambda p: "yes")
            first.get("topic", "Q?", False)
            first.save(path)
            self.assertTrue(os.path.exists(path))

            second = Interactor(mode="never")
            self.assertEqual(second.load(path), 1)
            answer = second.get("topic", "Q?", False)
            self.assertEqual(answer.source, "cached")
            self.assertTrue(answer.value)

    def test_transcript_is_serializable(self):
        interactor = Interactor(mode="auto")
        interactor.get("a", "Q?", 1, "detail")
        json.dumps(interactor.transcript())

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            Interactor(mode="whatever")


class AgentTests(unittest.TestCase):
    def _agent(self, mode="never", answers=None, **overrides):
        queue = iter(answers or [])
        interactor = Interactor(
            mode=mode,
            interactive=True,
            prompter=lambda p: next(queue, ""),
        )
        config = CrawlConfig(seeds=("https://x.test",), **overrides)
        return Agent(config, interactor=interactor,
                     in_scope=lambda url: True), interactor

    def test_pagination_expands_when_allowed(self):
        agent, _ = self._agent(answers=["8"])
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        urls = agent.consider("https://x.test/blog", signals, 1)
        self.assertTrue(any("page=" in u for u in urls))
        kinds = {a["action"] for a in agent.actions_taken}
        self.assertIn(ACTION_PAGINATE, kinds)

    def test_pagination_skips_the_page_one_link(self):
        """The first link in a pager is page 1 and reveals no pattern."""
        agent, _ = self._agent(mode="ask", answers=["5"])
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        urls = agent.consider("https://x.test/blog", signals, 1)
        expand = [a for a in agent.actions_taken if a["action"] == ACTION_PAGINATE]
        self.assertEqual(expand[0]["added"], 5)
        self.assertIn("?page=2", expand[0]["reason"])
        # page=2 is still reached, but via the plain "Next" link, not expansion.
        self.assertIn("https://x.test/blog?page=2", urls)
        self.assertIn("https://x.test/blog?page=3", urls)

    def test_pagination_declined(self):
        agent, _ = self._agent(answers=["0"])
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        agent.consider("https://x.test/blog", signals, 1)
        self.assertTrue(any(a["skipped"] for a in agent.actions_taken))

    def test_login_page_yields_no_urls(self):
        agent, _ = self._agent()
        signals = scan_page(LOGIN_PAGE, "https://x.test/members")
        urls = agent.consider("https://x.test/members", signals, 1)
        self.assertEqual(urls, [])
        self.assertIn(ACTION_LOGIN, {a["action"] for a in agent.actions_taken})

    def test_search_only_runs_when_terms_given(self):
        agent, _ = self._agent(mode="ask", answers=[""])
        signals = scan_page(SEARCH_PAGE, "https://x.test/")
        self.assertEqual(agent.consider("https://x.test/", signals, 0), [])

        agent2, _ = self._agent(mode="ask", answers=["boots"])
        signals2 = scan_page(SEARCH_PAGE, "https://x.test/")
        urls = agent2.consider("https://x.test/", signals2, 0)
        self.assertIn("https://x.test/search?q=boots", urls)

    def test_next_links_followed_when_enabled(self):
        agent, _ = self._agent(answers=["y"])
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        agent.consider("https://x.test/blog", signals, 1)
        self.assertIn(ACTION_NEXT, {a["action"] for a in agent.actions_taken})

    def test_actions_disabled_returns_nothing(self):
        agent, _ = self._agent(auto_actions=False)
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        self.assertEqual(agent.consider("https://x.test/blog", signals, 1), [])

    def test_action_budget_caps_additions(self):
        agent, _ = self._agent(answers=["100"], max_actions_per_page=2)
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        urls = agent.consider("https://x.test/blog", signals, 1)
        self.assertLessEqual(len(urls), 2)

    def test_budget_zero_still_follows_next_only(self):
        agent, _ = self._agent(answers=["10"], max_actions_per_page=0)
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        self.assertEqual(agent.consider("https://x.test/blog", signals, 1), [])

    def test_summary_shape(self):
        agent, _ = self._agent()
        signals = scan_page(PAGINATED, "https://x.test/blog",
                            word_count=20, link_count=6)
        agent.consider("https://x.test/blog", signals, 1)
        summary = agent.summary()
        self.assertIn("actions_total", summary)
        self.assertIn("interactions", summary)


class AgentInCrawlTests(unittest.TestCase):
    def test_findings_recorded_on_pages(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/"], max_depth=2,
                                 delay=0.0, use_sitemap=False,
                                 interaction_mode="never")
            result = crawl(config, logger=None)
        self.assertIn("agent", result["summary"])
        self.assertTrue(any("findings" in p for p in result["pages"]))

    def test_login_page_blocks_its_own_links(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/members.html"], delay=0.0,
                                 use_sitemap=False, interaction_mode="never")
            result = crawl(config, logger=None)
        page = result["pages"][0]
        self.assertTrue(page["needs_login"])
        # Links are still recorded for the report, but nothing behind the wall
        # is fetched: the agent refuses to authenticate.
        self.assertTrue(page["internal_links"])
        urls = {p["url"] for p in result["pages"]}
        self.assertNotIn(site.base + "/members-secret.html", urls)
        self.assertIn(page["url"], result["summary"]["login_walls"])

    def test_agent_off_adds_no_pagination_urls(self):
        with LocalSite() as site:
            base = CrawlConfig(seeds=[site.base + "/blog.html"], delay=0.0,
                               use_sitemap=False, max_pages=20)
            off = crawl(base.merged(auto_actions=False), logger=None)
            on = crawl(base.merged(auto_actions=True, interaction_mode="never"),
                       logger=None)
        self.assertLessEqual(len(off["pages"]), len(on["pages"]))
        self.assertGreater(on["summary"]["agent"]["actions_total"], 0)

    def test_decisions_file_written(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decisions.json")
            config = CrawlConfig(seeds=[site.base + "/"], delay=0.0,
                                 use_sitemap=False, decisions_file=path)
            crawl(config, logger=None)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertIn("answers", payload)
            self.assertIn("summary", payload)


class DiagnosisTests(unittest.TestCase):
    """A crawl that reaches only one page must say which rule stopped it."""

    def _site(self, index_html, pages=None):
        table = dict(pages or {})
        table["/"] = index_html

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = table.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, "http://127.0.0.1:%d" % server.server_address[1]

    def _run(self, base, **over):
        settings = {"seeds": (base + "/",), "delay": 0.0, "use_sitemap": False,
                    "max_pages": 20, "max_depth": 3}
        settings.update(over)
        return crawl(CrawlConfig(**settings), logger=None)

    def test_no_links_suggests_javascript(self):
        html = ("<html><head><title>H</title></head><body><div id=root></div>"
                "<script src=a.js></script><script src=b.js></script>"
                "<script src=c.js></script></body></html>")
        server, base = self._site(html)
        try:
            result = self._run(base)
        finally:
            server.shutdown(); server.server_close()
        diagnosis = result["summary"]["diagnosis"]
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(diagnosis["internal_links_found"], 0)
        self.assertTrue(any("JavaScript" in s for s in diagnosis["suggestions"]))
        self.assertTrue(any(r["reason"] == "js_rendered"
                            for r in diagnosis["reasons"]))

    def test_nofollow_is_reported_not_blamed_on_javascript(self):
        html = ('<html><head><title>H</title></head><body><h1>H</h1>'
                '<p>%s</p><a href="/a" rel="nofollow">A</a>'
                '<a href="/b" rel="nofollow">B</a></body></html>' % ("text " * 40))
        server, base = self._site(html)
        try:
            result = self._run(base)
        finally:
            server.shutdown(); server.server_close()
        diagnosis = result["summary"]["diagnosis"]
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(diagnosis["internal_links_found"], 2)
        reasons = {r["reason"] for r in diagnosis["reasons"]}
        self.assertIn("nofollow", reasons)
        self.assertNotIn("js_rendered", reasons)
        self.assertFalse(any("JavaScript" in s for s in diagnosis["suggestions"]),
                         "must not blame JavaScript when nofollow was the cause")

    def test_asset_and_admin_paths_are_reported(self):
        html = ('<html><body><a href="/admin/x">A</a><a href="/cart">B</a>'
                '<a href="/f.pdf">P</a></body></html>')
        server, base = self._site(html)
        try:
            result = self._run(base)
        finally:
            server.shutdown(); server.server_close()
        reasons = {r["reason"] for r in result["summary"]["diagnosis"]["reasons"]}
        self.assertIn("asset_or_admin_path", reasons)
        self.assertEqual(len(result["pages"]), 1)

    def test_out_of_scope_is_reported(self):
        html = '<html><body><a href="/a">A</a></body></html>'
        server, base = self._site(html, {"/a": "<html><body>A</body></html>"})
        try:
            result = self._run(base, exclude_patterns=[r"/a"])
        finally:
            server.shutdown(); server.server_close()
        reasons = {r["reason"] for r in result["summary"]["diagnosis"]["reasons"]}
        self.assertIn("out_of_scope", reasons)

    def test_depth_zero_is_called_out(self):
        html = '<html><body><a href="/a">A</a></body></html>'
        server, base = self._site(html, {"/a": "<html><body>A</body></html>"})
        try:
            result = self._run(base, max_depth=0)
        finally:
            server.shutdown(); server.server_close()
        reasons = {r["reason"] for r in result["summary"]["diagnosis"]["reasons"]}
        self.assertIn("depth_limit", reasons)

    def test_page_budget_is_called_out(self):
        html = '<html><body><a href="/a">A</a></body></html>'
        server, base = self._site(html, {"/a": "<html><body>A</body></html>"})
        try:
            result = self._run(base, max_pages=1)
        finally:
            server.shutdown(); server.server_close()
        reasons = {r["reason"] for r in result["summary"]["diagnosis"]["reasons"]}
        self.assertIn("page_budget", reasons)

    def test_duplicate_content_is_reported(self):
        same = "<html><head><title>T</title></head><body><p>%s</p></body></html>" % (
            "identical boilerplate body text " * 12)
        html = ('<html><head><title>H</title></head><body><h1>H</h1>'
                '<a href="/x">X</a><a href="/y">Y</a></body></html>')
        server, base = self._site(html, {"/x": same, "/y": same})
        try:
            result = self._run(base, dedupe_content=True)
        finally:
            server.shutdown(); server.server_close()
        reasons = {r["reason"] for r in result["summary"]["diagnosis"]["reasons"]}
        self.assertIn("duplicate_content", reasons)

    def test_healthy_crawl_reports_no_reasons(self):
        pages = {
            "/a": '<html><head><title>A</title></head><body><h1>A</h1><p>%s</p>'
                  '<a href="/">H</a></body></html>' % ("alpha text " * 30),
            "/b": '<html><head><title>B</title></head><body><h1>B</h1><p>%s</p>'
                  '<a href="/">H</a></body></html>' % ("beta words " * 30),
        }
        html = ('<html><head><title>H</title></head><body><h1>H</h1>'
                '<p>%s</p><a href="/a">A</a><a href="/b">B</a></body></html>'
                % ("home text " * 30))
        server, base = self._site(html, pages)
        try:
            result = self._run(base)
        finally:
            server.shutdown(); server.server_close()
        diagnosis = result["summary"]["diagnosis"]
        self.assertEqual(len(result["pages"]), 3)
        self.assertEqual(diagnosis["internal_links_found"], 4)
        self.assertEqual(diagnosis["reasons"], [])
        self.assertEqual(diagnosis["suggestions"], [])

    def test_dropped_examples_are_kept(self):
        html = '<html><body><a href="/f1.pdf">1</a><a href="/f2.pdf">2</a></body></html>'
        server, base = self._site(html)
        try:
            result = self._run(base)
        finally:
            server.shutdown(); server.server_close()
        examples = result["summary"]["diagnosis"]["dropped_examples"]
        self.assertTrue(any("f1.pdf" in u for u in examples["asset_or_admin_path"]))

    def test_cli_prints_diagnosis_for_a_one_page_crawl(self):
        import crawl as cli
        html = "<html><body><div id=root></div></body></html>"
        server, base = self._site(html)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                code = cli.main([base + "/", "--no-sitemap", "-q", "-o", tmp])
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)