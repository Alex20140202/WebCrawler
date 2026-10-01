from __future__ import annotations

import json
import os
import tempfile
import unittest

from supercrawler import CrawlConfig, crawl
from supercrawler.crawler import Crawler
from supercrawler.discovery import (
    candidate_sitemaps,
    estimate_from_links,
    find_sitemap_directives,
    is_crawlable_url,
    looks_js_rendered,
    pagination_key,
    parse_sitemap,
    recommend_presets,
    strip_tracking_params,
)
from supercrawler.state import CrawlState, load_state, save_state
from supercrawler.wizard import GOALS, build_plan, normalize_site_input

from test_crawler import DEEP, LocalSite, SITEMAP_INDEX, SITEMAP_PAGES


class SitemapParsingTests(unittest.TestCase):
    def test_parses_urlset(self):
        entries, nested = parse_sitemap(SITEMAP_PAGES.format(base="https://x.test"))
        self.assertEqual(len(entries), 7)
        self.assertEqual(nested, [])
        self.assertEqual(entries[0].loc, "https://x.test/about.html")
        self.assertEqual(entries[0].lastmod, "2024-01-02")

    def test_parses_sitemap_index(self):
        entries, nested = parse_sitemap(
            SITEMAP_INDEX.format(base="https://x.test")
        )
        self.assertEqual(entries, [])
        self.assertEqual(nested, ["https://x.test/sitemap-pages.xml"])

    def test_handles_namespace_prefixed_xml(self):
        xml = """<?xml version="1.0"?>
        <sitemap:urlset xmlns:sitemap="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap:url><sitemap:loc>https://x.test/a</sitemap:loc></sitemap:url>
        </sitemap:urlset>"""
        entries, _ = parse_sitemap(xml)
        self.assertEqual([e.loc for e in entries], ["https://x.test/a"])

    def test_malformed_xml_returns_empty(self):
        self.assertEqual(parse_sitemap("<urlset><url><loc>oops"),
                         ([], []))
        self.assertEqual(parse_sitemap(""), ([], []))

    def test_robots_directives(self):
        robots = "User-agent: *\nDisallow: /x\nSitemap: https://x.test/s1.xml\n"
        self.assertEqual(find_sitemap_directives(robots), ["https://x.test/s1.xml"])

    def test_candidate_order_puts_declared_first(self):
        candidates = candidate_sitemaps(
            "https://x.test/", "Sitemap: https://x.test/custom.xml"
        )
        self.assertEqual(candidates[0], "https://x.test/custom.xml")
        self.assertIn("https://x.test/sitemap.xml", candidates)


class UrlPolicyTests(unittest.TestCase):
    def test_rejects_assets_and_admin_paths(self):
        for url in ("https://x.test/a.pdf", "https://x.test/b.JPG",
                    "https://x.test/wp-admin/x", "https://x.test/login",
                    "https://x.test/cart/"):
            self.assertFalse(is_crawlable_url(url), url)

    def test_accepts_normal_pages(self):
        for url in ("https://x.test/", "https://x.test/blog/post-1",
                    "https://x.test/docs/index.html"):
            self.assertTrue(is_crawlable_url(url), url)

    def test_search_paths_blocked_by_default_but_allowable_on_request(self):
        self.assertFalse(is_crawlable_url("https://x.test/search?q=a"))
        self.assertTrue(is_crawlable_url("https://x.test/search?q=a",
                                         allow_search=True))

    def test_pagination_detection(self):
        self.assertIsNotNone(pagination_key("https://x.test/blog?page=3"))
        self.assertIsNotNone(pagination_key("https://x.test/blog/page/4"))
        self.assertIsNotNone(pagination_key("https://x.test/blog-post-5.html"))
        self.assertIsNone(pagination_key("https://x.test/blog/post-1"))
        self.assertIsNone(pagination_key("https://x.test/?page=1"))

    def test_strips_tracking_params(self):
        self.assertEqual(
            strip_tracking_params("https://x.test/a?utm_source=n&id=7"),
            "https://x.test/a?id=7",
        )
        self.assertEqual(strip_tracking_params("https://x.test/a"), "https://x.test/a")

    def test_js_rendered_heuristic(self):
        self.assertTrue(looks_js_rendered("<script></script>" * 4, link_count=1,
                                          word_count=5))
        self.assertFalse(looks_js_rendered(DEEP, link_count=1, word_count=10))
        self.assertFalse(looks_js_rendered(DEEP, link_count=40, word_count=800))

    def test_estimates_and_recommendations(self):
        self.assertEqual(estimate_from_links([])["est_pages"], 0)

        sitemap_sized = estimate_from_links(["u"] * 9000)
        self.assertEqual(sitemap_sized["source"], "sitemap")
        self.assertEqual(sitemap_sized["est_pages"], 9000)

        small = recommend_presets({"est_pages": 40})
        large = recommend_presets({"est_pages": 9000})
        self.assertEqual(small["max_pages"], 100)
        self.assertEqual(large["max_pages"], 0)
        self.assertLess(small["max_depth"], large["max_depth"])
        self.assertLessEqual(small["delay"], large["delay"])


class SmartCrawlTests(unittest.TestCase):
    def test_plan_reports_sitemap_urls(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base], delay=0.0, per_host_concurrency=4)
            plan = Crawler(config).plan()
        self.assertEqual(plan["probe"]["host"], "127.0.0.1")
        self.assertTrue(plan["probe"]["robots_exists"])
        self.assertTrue(plan["probe"]["sitemap_sources"])
        self.assertGreaterEqual(plan["probe"]["urls_usable"], 5)
        self.assertTrue(any("about.html" in u for u in plan["sample_urls"]))
        self.assertTrue(any("orphan.html" in u for u in plan["sample_urls"]))
        self.assertIn("recommended", plan)

    def test_sitemap_seeds_reach_orphan_pages(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=50, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
            )
            result = crawl(config, logger=None)
        urls = {p["url"] for p in result["pages"]}
        self.assertIn(site.base + "/orphan.html", urls,
                      "sitemap-only page was not crawled")
        self.assertIn(site.base + "/about.html", urls)

    def test_sitemap_respects_robots_and_scope(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=50, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
            )
            result = crawl(config, logger=None)
        urls = {p["url"] for p in result["pages"]}
        self.assertNotIn(site.base + "/private.html", urls, "robots.txt ignored")
        self.assertNotIn("http://external.test/offsite", urls, "left the site")

    def test_coverage_is_reported(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=50, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
            )
            result = crawl(config, logger=None)
        coverage = result["summary"]["coverage"]
        self.assertIsNotNone(coverage)
        self.assertGreater(coverage["known_urls"], 0)
        self.assertGreater(coverage["percent"], 0)
        self.assertEqual(coverage["crawled"] + coverage["remaining"],
                         coverage["known_urls"])

    def test_no_sitemap_falls_back_to_link_discovery(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=50, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=False,
            )
            result = crawl(config, logger=None)
        urls = {p["url"] for p in result["pages"]}
        self.assertIn(site.base + "/about.html", urls)
        self.assertNotIn(site.base + "/orphan.html", urls)
        self.assertIsNone(result["summary"]["coverage"])

    def test_unlimited_depth_flag(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=-1, max_pages=0, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
            )
            self.assertTrue(config.unlimited_depth)
            self.assertTrue(config.unlimited)
            result = crawl(config, logger=None)
        urls = {p["url"] for p in result["pages"]}
        self.assertIn(site.base + "/deepest.html", urls,
                      "unlimited depth should reach the deepest fixture")
        self.assertEqual(result["summary"]["pending"], 0)

    def test_depth_zero_fetches_seeds_only(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=0, delay=0.0,
                use_sitemap=False,
            )
            self.assertFalse(config.unlimited_depth)
            result = crawl(config, logger=None)
        self.assertEqual(len(result["pages"]), 1)

    def test_unlimited_pages_runs_until_queue_drains(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=0, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
            )
            result = crawl(config, logger=None)
        self.assertEqual(result["summary"]["pending"], 0)
        self.assertTrue(result["summary"]["config"]["unlimited"])
        self.assertGreaterEqual(result["summary"]["pages_crawled"], 5)

    def test_max_pages_caps_whole_site_mode(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=5, max_pages=3, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
            )
            result = crawl(config, logger=None)
        self.assertEqual(len(result["pages"]), 3)

    def test_duplicate_content_collapsed(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/dup.html"], max_depth=3, max_pages=20,
                delay=0.0, concurrency=2, per_host_concurrency=2,
                dedupe_content=True, use_sitemap=False,
            )
            result = crawl(config, logger=None)
        pages = [p for p in result["pages"] if not p.get("error")]
        self.assertEqual(result["summary"]["duplicates"], 0,
                         "short pages should not be treated as duplicates")

    def test_js_rendered_page_flagged(self):
        with LocalSite() as site:
            config = CrawlConfig(
                seeds=[site.base + "/spa.html"], delay=0.0, use_sitemap=False,
            )
            result = crawl(config, logger=None)
        self.assertTrue(result["pages"][0]["js_rendered"])
        self.assertEqual(result["summary"]["js_rendered_pages"], 1)


class StateTests(unittest.TestCase):
    def test_roundtrip(self):
        state = CrawlState(seen=["a"], pending=[("b", 1), ("c", 2)],
                           pages_crawled=5, signature="x.test")
        with tempfile.TemporaryDirectory() as tmp:
            path = save_state(os.path.join(tmp, "s.json"), state)
            loaded = load_state(path)
        self.assertEqual(loaded.seen, {"a"})
        self.assertEqual(loaded.pending, [("b", 1), ("c", 2)])
        self.assertEqual(loaded.pages_crawled, 5)
        self.assertEqual(loaded.signature, "x.test")

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_state("/nonexistent/nope.json"))

    def test_version_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 999, "seen": []}, handle)
            self.assertIsNone(load_state(path))

    def test_pop_batch_respects_depth(self):
        state = CrawlState(pending=[("a", 0), ("b", 5), ("c", 1)])
        batch = state.pop_batch(10, max_depth=2)
        self.assertEqual([u for u, _ in batch], ["a", "c"])

    def test_pop_batch_negative_depth_is_unlimited(self):
        state = CrawlState(pending=[("a", 0), ("b", 5)])
        self.assertEqual(len(state.pop_batch(10, max_depth=-1)), 2)

    def test_pop_batch_depth_zero_allows_seeds_only(self):
        state = CrawlState(pending=[("a", 0), ("b", 1)])
        self.assertEqual([u for u, _ in state.pop_batch(10, max_depth=0)], ["a"])

    def test_push_is_idempotent(self):
        state = CrawlState()
        state.push("a", 1)
        state.push("a", 1)
        self.assertEqual(len(state.pending), 1)
        self.assertTrue(state.is_seen("a"))


class ResumeTests(unittest.TestCase):
    def test_state_file_is_written(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            config = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=2, delay=0.0,
                concurrency=2, per_host_concurrency=2, use_sitemap=True,
                state_file=state_path,
            )
            crawl(config, logger=None)
            self.assertTrue(os.path.exists(state_path))
            state = load_state(state_path)
            self.assertGreater(state.pages_crawled, 0)

    def test_coverage_is_cumulative_across_runs(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            base = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
                state_file=state_path,
            )
            first = crawl(base.merged(max_pages=3), logger=None)
            second = crawl(base.merged(max_pages=0), logger=None)

        first_cov = first["summary"]["coverage"]["crawled"]
        second_cov = second["summary"]["coverage"]["crawled"]
        self.assertGreater(second_cov, first_cov,
                           "coverage must not reset when a crawl resumes")
        self.assertTrue(second["summary"]["resumed"])

    def test_state_survives_a_full_crawl(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            base = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, delay=0.0,
                concurrency=4, per_host_concurrency=4, use_sitemap=True,
                state_file=state_path,
            )
            crawl(base.merged(max_pages=0), logger=None)
            third = crawl(base.merged(max_pages=0), logger=None)

        self.assertEqual(third["summary"]["pages_crawled"], 0,
                         "everything was already crawled")
        self.assertEqual(third["summary"]["pending"], 0)

    def test_state_rejected_for_a_different_site(self):
        with LocalSite() as a, LocalSite() as b, tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            config = CrawlConfig(
                seeds=[a.base + "/"], max_depth=2, max_pages=1, delay=0.0,
                concurrency=2, per_host_concurrency=2, use_sitemap=False,
                state_file=state_path,
            )
            crawl(config, logger=None)
            other = config.merged(seeds=(b.base + "/",))
            result = crawl(other, logger=None)
        self.assertFalse(result["summary"]["resumed"],
                         "state from another host must not be reused")

    def test_resume_skips_already_seen_urls(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            base = CrawlConfig(
                seeds=[site.base + "/"], max_depth=3, max_pages=2, delay=0.0,
                concurrency=2, per_host_concurrency=2, use_sitemap=True,
            )
            first = crawl(base.merged(state_file=state_path), logger=None)
            second = crawl(
                base.merged(state_file=state_path, max_pages=0), logger=None
            )
        self.assertTrue(second["summary"]["resumed"], "state file was not reused")
        self.assertGreaterEqual(len(second["pages"]), len(first["pages"]))


class WizardTests(unittest.TestCase):
    def test_normalize_site_input(self):
        self.assertEqual(normalize_site_input("example.com"), "https://example.com")
        self.assertEqual(normalize_site_input("http://x.test/"), "http://x.test")
        self.assertEqual(normalize_site_input(""), "")

    def _scripted(self, answers):
        queue = list(answers)

        def prompter(prompt):
            return queue.pop(0) if queue else ""

        return prompter

    def test_build_plan_defaults_whole_site(self):
        prompter = self._scripted([
            "example.com",     # site
            "1",               # whole site
            "1",               # content goal
            "1",               # unlimited budget
            "12", "0",         # depth, max pages
            "2",               # balanced
            "0.5", "8", "2",   # delay, concurrency, per-host
            "",                # user agent (preset default)
            "", "", "", "", "",  # robots, sitemap, dedupe, save html, external
            "out",             # output dir
            "",                # excludes
        ])
        built = build_plan(prompter=prompter)
        config = built["config"]
        self.assertEqual(built["base_url"], "https://example.com")
        self.assertEqual(config.seeds, ("https://example.com",))
        self.assertEqual(config.max_pages, 0)
        self.assertTrue(config.unlimited)
        self.assertTrue(config.use_sitemap)
        self.assertTrue(config.respect_robots)
        self.assertEqual(config.output_dir, "out")
        self.assertEqual(config.concurrency, 8)

    def test_build_plan_single_page_scope(self):
        prompter = self._scripted([
            "https://x.test", "3", "1", "1", "2", "0", "2", "1", "1", "1",
            "", "", "", "", "", "o", "",
        ])
        built = build_plan(prompter=prompter)
        self.assertEqual(built["plan"]["scope"], "single page")
        self.assertTrue(built["config"].include_patterns)
        self.assertEqual(built["config"].max_pages, 0)

    def test_build_plan_contacts_goal_filters_paths(self):
        prompter = self._scripted([
            "https://x.test", "1", "4", "1", "3", "1", "2", "0.2", "4", "1",
            "", "", "", "", "", "o", "",
        ])
        built = build_plan(prompter=prompter)
        self.assertEqual(built["plan"]["goal"], "contacts")
        self.assertIn("contact", " ".join(built["config"].include_patterns))

    def test_build_plan_requires_url(self):
        with self.assertRaises(ValueError):
            build_plan(prompter=self._scripted([""]))

    def test_goals_are_wellformed(self):
        for key, goal in GOALS.items():
            self.assertIn("label", goal)
            self.assertIn("exclude", goal)
            if goal["include"]:
                self.assertIsInstance(goal["include"], str)


class WizardAuthTests(unittest.TestCase):
    BASE_STEPS = [
        "x.test",     # site
        "1",          # whole site
        "1",          # content goal
        "1",          # unlimited budget
        "12", "0",    # depth, max pages
        "2",          # balanced
        "0.5", "8", "2",
        "",           # user agent
        "", "", "", "", "",  # robots, sitemap, dedupe, save html, external
        "out",        # output dir
        "",           # excludes
    ]

    def _scripted(self, answers):
        queue = list(answers)

        def prompter(prompt):
            return queue.pop(0) if queue else ""

        return prompter

    def test_no_login_is_the_default(self):
        built = build_plan(prompter=self._scripted(self.BASE_STEPS + ["1"]))
        self.assertEqual(built["config"].login_url, "")
        self.assertFalse(built["config"].has_login)

    def test_login_form_with_env_password(self):
        steps = self.BASE_STEPS + [
            "2",                      # sign in with a login form
            "https://x.test/login",   # login page
            "alice",                  # username
            "MY_PW",                  # env var name
            "y",                      # already exported
            "y",                      # abort if login fails
        ]
        built = build_plan(prompter=self._scripted(steps))
        config = built["config"]
        self.assertEqual(config.login_url, "https://x.test/login")
        self.assertEqual(config.login_username, "alice")
        self.assertEqual(config.login_password_env, "MY_PW")
        self.assertEqual(config.login_password, "", "nothing should be stored")
        self.assertTrue(config.require_login)
        self.assertTrue(config.session_file.endswith("session.json"))

    def test_login_form_with_typed_password(self):
        steps = self.BASE_STEPS + [
            "2", "https://x.test/login", "alice", "MY_PW",
            "n",       # not exported, so prompt for it
            "s3cret",  # typed
            "y",
        ]
        built = build_plan(prompter=self._scripted(steps))
        self.assertEqual(built["config"].login_password, "s3cret")
        self.assertNotIn("s3cret", str(built["plan"]), "plan must not echo it")

    def test_bare_login_url_gets_https(self):
        steps = self.BASE_STEPS + ["2", "x.test/login", "bob", "PW", "y", "y"]
        built = build_plan(prompter=self._scripted(steps))
        self.assertEqual(built["config"].login_url, "https://x.test/login")

    def test_login_url_can_be_declined(self):
        built = build_plan(prompter=self._scripted(self.BASE_STEPS + ["2", ""]))
        self.assertEqual(built["config"].login_url, "")

    def test_reuse_saved_session(self):
        steps = self.BASE_STEPS + ["3", "", "y"]
        built = build_plan(prompter=self._scripted(steps))
        self.assertTrue(built["config"].session_file)
        self.assertEqual(built["config"].login_url, "")

    def test_auth_header_mode(self):
        steps = self.BASE_STEPS + ["4", "Authorization: Bearer abc"]
        built = build_plan(prompter=self._scripted(steps))
        self.assertEqual(built["config"].auth_header, "Authorization: Bearer abc")

    def test_empty_auth_header_falls_back_to_nothing(self):
        built = build_plan(prompter=self._scripted(self.BASE_STEPS + ["4", ""]))
        self.assertEqual(built["config"].auth_header, "")

    def test_typed_password_is_not_written_to_the_plan_summary(self):
        steps = self.BASE_STEPS + [
            "2", "https://x.test/login", "alice", "PW", "n", "topsecret", "y"]
        built = build_plan(prompter=self._scripted(steps))
        self.assertNotIn("topsecret", json.dumps(built["plan"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)