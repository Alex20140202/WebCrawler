from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from supercrawler import CrawlConfig, crawl
from supercrawler.auth import (
    AuthError,
    Authenticator,
    describe_auth,
    find_login_form,
    has_captcha,
    looks_logged_in,
    needs_totp,
    pick_field,
    redact,
    save_cookies,
    load_cookies,
)
from supercrawler.fetcher import Fetcher

from test_crawler import (
    CAPTCHA_PAGE,
    DASHBOARD,
    LOGIN_PAGE,
    OK_PASS,
    OK_USER,
    TOTP_CODE,
    TOTP_PAGE,
    LocalSite,
)


class FormDetectionTests(unittest.TestCase):
    def test_finds_login_form_and_csrf(self):
        form = find_login_form(LOGIN_PAGE % {"error": "", "action": "/login"}, "https://x.test/login")
        self.assertIsNotNone(form)
        self.assertEqual(form["method"], "post")
        self.assertIn("username", form["fields"])
        self.assertIn("password", form["fields"])
        self.assertEqual(form["csrf"]["csrfmiddlewaretoken"], "tok-abc-123")

    def test_search_form_is_not_mistaken_for_login(self):
        html = ('<form action="/search" method="get">'
                '<input name="q"><input type="submit"></form>')
        self.assertIsNone(find_login_form(html, "https://x.test/"))

    def test_pick_field_prefers_exact_then_scored_match(self):
        from supercrawler.auth import PASSWORD_FIELDS, USERNAME_FIELDS
        names = {"user_email": "text", "pwd": "password"}
        self.assertEqual(pick_field(names, USERNAME_FIELDS), "user_email")
        self.assertEqual(pick_field(names, PASSWORD_FIELDS), "pwd")
        self.assertEqual(pick_field({"login": "text"}, ("username", "login")),
                         "login")
        self.assertIsNone(pick_field({"x": "y"}, ("password",)))

    def test_totp_page_does_not_look_logged_in(self):
        self.assertFalse(looks_logged_in(TOTP_PAGE, "https://x.test/2fa",
                                         "https://x.test/login"))

    def test_code_form_finder_matches_a_2fa_step(self):
        from supercrawler.auth import find_code_form
        form = find_code_form(TOTP_PAGE, "https://x.test/2fa")
        self.assertIsNotNone(form)
        self.assertEqual(form["code_field"], "otp")
        self.assertEqual(form["action"], "https://x.test/2fa")
        self.assertEqual(form["csrf"]["csrfmiddlewaretoken"], "tok-2fa")

    def test_totp_detection(self):
        self.assertTrue(needs_totp(TOTP_PAGE))
        self.assertFalse(needs_totp(LOGIN_PAGE % {"error": "", "action": "/login"}))

    def test_captcha_detection(self):
        self.assertTrue(has_captcha(CAPTCHA_PAGE))
        self.assertFalse(has_captcha(DASHBOARD))

    def test_logged_in_detection(self):
        self.assertTrue(looks_logged_in(DASHBOARD, "https://x.test/dashboard",
                                        "https://x.test/login"))
        self.assertFalse(looks_logged_in(LOGIN_PAGE % {"error": "", "action": "/login"},
                                         "https://x.test/login",
                                         "https://x.test/login"))
        self.assertFalse(looks_logged_in(
            LOGIN_PAGE % {"error": "<p>Invalid password.</p>", "action": "/login"},
            "https://x.test/login", "https://x.test/login"))


class RedactionTests(unittest.TestCase):
    def test_password_never_appears_in_log_text(self):
        message = "submitting password=hunter2 token=abc123 user=bob"
        cleaned = redact(message)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertIn("bobby" if False else "bob", cleaned)

    def test_describe_auth_omits_the_password(self):
        config = CrawlConfig(login_url="https://x.test/login",
                             login_username="bob",
                             login_password="hunter2")
        described = describe_auth(config)
        blob = json.dumps(described)
        self.assertNotIn("hunter2", blob)
        self.assertEqual(described["username"], "bob")
        self.assertEqual(described["password_source"], "inline")

    def test_describe_auth_reports_env_source(self):
        config = CrawlConfig(login_url="https://x.test/login",
                             login_password_env="MY_PW")
        self.assertEqual(describe_auth(config)["password_source"],
                         "environment:MY_PW")


class CookiePersistenceTests(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "session.json")
            session = requests.Session()
            session.cookies.set("sid", "abc", domain="x.test", path="/")
            save_cookies(session, path)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600,
                             "session file must not be world-readable")

            fresh = requests.Session()
            self.assertEqual(load_cookies(fresh, path), 1)
            self.assertEqual(fresh.cookies.get("sid"), "abc")

    def test_missing_file_is_not_an_error(self):
        import requests
        self.assertEqual(load_cookies(requests.Session(), "/nope/x.json"), 0)

    def test_corrupt_file_is_not_an_error(self):
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(load_cookies(requests.Session(), path), 0)


class AuthenticatorTests(unittest.TestCase):
    def _auth(self, site, tmp, **overrides):
        config = CrawlConfig(
            login_url=site.base + "/login",
            login_username=OK_USER,
            login_password=OK_PASS,
            session_file=os.path.join(tmp, "session.json"),
            **overrides
        )
        fetcher = Fetcher(config)
        return fetcher, Authenticator(fetcher.session, config)

    def test_successful_login_sets_session(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher, auth = self._auth(site, tmp)
            result = auth.login()
            self.assertTrue(result.ok, result.reason)
            self.assertEqual(result.method, "form")
            page = fetcher.session.get(site.base + "/dashboard", timeout=5)
            self.assertEqual(page.status_code, 200)
            self.assertIn("Dashboard", page.text)
            self.assertTrue(os.path.exists(auth.config.session_file))

    def test_wrong_password_reports_failure(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher, _ = self._auth(site, tmp)
            auth = Authenticator(fetcher.session, fetcher.config.merged(
                login_password="wrong-password"))
            result = auth.login()
            self.assertFalse(result.ok)
            self.assertIn("rejected", result.reason)
            self.assertNotIn("wrong-password", result.reason)

    def test_missing_credentials_do_not_post(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            config = CrawlConfig(login_url=site.base + "/login",
                                 login_username=OK_USER,
                                 login_password_env="DEFINITELY_UNSET_PW")
            fetcher = Fetcher(config)
            result = Authenticator(fetcher.session, config).login()
            self.assertFalse(result.ok)
            self.assertIn("missing credentials", result.reason)

    def test_captcha_is_refused_not_solved(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher = Fetcher(CrawlConfig(login_url=site.base + "/captcha-login",
                                          login_username=OK_USER,
                                          login_password=OK_PASS))
            result = Authenticator(fetcher.session, fetcher.config).login()
            self.assertFalse(result.ok)
            self.assertTrue(result.needs_captcha)
            self.assertIn("captcha", result.reason)

    def test_two_factor_uses_supplied_code(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher = Fetcher(CrawlConfig(
                login_url=site.base + "/login-totp",
                login_username=OK_USER, login_password=OK_PASS,
                session_file=os.path.join(tmp, "s.json")))
            auth = Authenticator(fetcher.session, fetcher.config)
            answer = iter([TOTP_CODE])
            auth.prompt_code = lambda message: next(answer, "")
            result = auth.login()
            self.assertTrue(result.ok, result.reason)
            self.assertEqual(result.method, "form+totp")

    def test_two_factor_without_code_fails_cleanly(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher = Fetcher(CrawlConfig(
                login_url=site.base + "/login-totp",
                login_username=OK_USER, login_password=OK_PASS))
            auth = Authenticator(fetcher.session, fetcher.config)
            auth.prompt_code = lambda message: ""
            result = auth.login()
            self.assertFalse(result.ok)
            self.assertTrue(result.needs_totp)
            self.assertIn("second factor", result.reason)

    def test_wrong_two_factor_code_rejected(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher = Fetcher(CrawlConfig(
                login_url=site.base + "/login-totp",
                login_username=OK_USER, login_password=OK_PASS))
            auth = Authenticator(fetcher.session, fetcher.config)
            auth.prompt_code = lambda message: "000000"
            result = auth.login()
            self.assertFalse(result.ok)
            self.assertNotIn("000000", result.reason)

    def test_saved_session_is_reused_without_logging_in_again(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher, auth = self._auth(site, tmp)
            self.assertTrue(auth.login().ok)

            # A second authenticator with no credentials still works.
            config = CrawlConfig(session_file=auth.config.session_file)
            second = Fetcher(config)
            info = Authenticator(second.session, config).prepare()
            self.assertEqual(info["session_restored"], 1)
            page = second.session.get(site.base + "/reports.html", timeout=5)
            self.assertEqual(page.status_code, 200)

    def test_auth_header_is_applied(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            fetcher = Fetcher(CrawlConfig(auth_header="Authorization: Bearer tok-xyz"))
            info = Authenticator(fetcher.session, fetcher.config).prepare()
            self.assertTrue(info["header"])
            self.assertEqual(fetcher.session.headers["Authorization"],
                             "Bearer tok-xyz")


class CrawlWithLoginTests(unittest.TestCase):
    def test_authenticated_pages_are_crawled(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            config = CrawlConfig(
                seeds=[site.base + "/dashboard"],
                login_url=site.base + "/login",
                login_username=OK_USER,
                login_password=OK_PASS,
                session_file=os.path.join(tmp, "session.json"),
                delay=0.0, use_sitemap=False, require_login=True,
                allowed_domains=("127.0.0.1",),
            )
            result = crawl(config, logger=None)
        urls = {p["url"] for p in result["pages"]}
        self.assertIn(site.base + "/reports.html", urls)
        self.assertTrue(result["summary"]["auth"]["ok"])
        self.assertEqual(result["summary"]["auth"]["method"], "form")

    def test_without_login_protected_pages_redirect(self):
        with LocalSite() as site:
            config = CrawlConfig(seeds=[site.base + "/dashboard"], delay=0.0,
                                 use_sitemap=False, allowed_domains=("127.0.0.1",))
            result = crawl(config, logger=None)
        titles = [p.get("title") for p in result["pages"]]
        self.assertIn("Sign in", titles)
        self.assertNotIn("Dashboard", titles)

    def test_require_login_raises_when_credentials_fail(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            config = CrawlConfig(
                seeds=[site.base + "/dashboard"],
                login_url=site.base + "/login",
                login_username=OK_USER,
                login_password="nope",
                require_login=True, delay=0.0, use_sitemap=False,
                allowed_domains=("127.0.0.1",),
            )
            with self.assertRaises(AuthError):
                crawl(config, logger=None)

    def test_failed_login_without_require_still_produces_report(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            config = CrawlConfig(
                seeds=[site.base + "/dashboard"],
                login_url=site.base + "/login",
                login_username=OK_USER,
                login_password="nope",
                delay=0.0, use_sitemap=False, allowed_domains=("127.0.0.1",),
            )
            result = crawl(config, logger=None)
        self.assertFalse(result["summary"]["auth"]["ok"])
        self.assertTrue(result["pages"])

    def test_auth_summary_never_leaks_password(self):
        with LocalSite() as site, tempfile.TemporaryDirectory() as tmp:
            config = CrawlConfig(
                seeds=[site.base + "/dashboard"],
                login_url=site.base + "/login",
                login_username=OK_USER,
                login_password=OK_PASS,
                session_file=os.path.join(tmp, "session.json"),
                delay=0.0, use_sitemap=False,
            )
            result = crawl(config, logger=None)
        self.assertNotIn(OK_PASS, json.dumps(result["summary"]))

    def test_inline_password_wins_over_a_set_env_var(self):
        """An env var being present must not break an explicit inline password."""
        with mock.patch.dict(os.environ, {"SUPERCRAWLER_PASSWORD": "from-env"}):
            config = CrawlConfig(login_url="https://x.test/login",
                                 login_username="u", login_password="inline")
            config.validate()
            import requests
            fetcher = Fetcher(config)
            authenticator = Authenticator(fetcher.session, config)
            self.assertEqual(authenticator.password(), "inline")

    def test_password_source_reported_honestly(self):
        config = CrawlConfig(login_url="https://x.test/login",
                             login_username="u", login_password="inline")
        self.assertEqual(describe_auth(config)["password_source"], "inline")
        env_only = CrawlConfig(login_url="https://x.test/login",
                               login_username="u",
                               login_password_env="MY_PW")
        self.assertEqual(describe_auth(env_only)["password_source"],
                         "environment:MY_PW")

    def test_password_env_name_has_a_default(self):
        self.assertEqual(CrawlConfig().password_env_name, "SUPERCRAWLER_PASSWORD")
        self.assertEqual(CrawlConfig(login_password_env="X").password_env_name, "X")

    def test_config_rejects_malformed_auth_header(self):
        config = CrawlConfig(auth_header="just-a-token")
        with self.assertRaises(ValueError):
            config.validate()

    def test_has_login_flag(self):
        self.assertFalse(CrawlConfig().has_login)
        self.assertTrue(CrawlConfig(login_url="https://x.test/login").has_login)
        self.assertTrue(CrawlConfig(auth_header="A: b").has_login)
        self.assertTrue(CrawlConfig(session_file="/tmp/s.json").has_login)


if __name__ == "__main__":
    unittest.main(verbosity=2)