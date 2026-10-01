from __future__ import annotations

import json
import os
import shutil
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from http.server import ThreadingHTTPServer

from crawl import main
from test_crawler import OK_PASS, OK_USER, Handler

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_output")
shutil.rmtree(out, ignore_errors=True)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
Handler.port = server.server_address[1]
server.session_id = "smokesession"
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % Handler.port

print("### crawl.py --help")
try:
    main(["--help"])
except SystemExit as exc:
    print("  SystemExit code %s (expected 0)" % exc.code)
    assert exc.code == 0

print("\n### balanced crawl of %s/" % base)
code = main([base + "/", "--preset", "fast", "--max-depth", "2",
             "--output", out, "--save-html"])
assert code == 0, "crawl failed with %s" % code

for name in ("report.json", "report.html", "report.csv", "pages.jsonl"):
    path = os.path.join(out, name)
    print("  %-14s exists=%s size=%d" % (
        name, os.path.exists(path), os.path.getsize(path) if os.path.exists(path) else -1))
    assert os.path.exists(path), name

print("\n### filtered crawl (include pattern)")
out2 = out + "_filtered"
shutil.rmtree(out2, ignore_errors=True)
code = main([base + "/", "--include", "about", "--max-pages", "10", "-o", out2])
assert code == 0

print("\n### ignore-robots crawl of blocked page")
out3 = out + "_robots"
shutil.rmtree(out3, ignore_errors=True)
code = main([base + "/private.html", "--ignore-robots", "-o", out3])
assert code == 0

print("\n### bad seed error handling")
code = main(["mailto:nope@example.com", "-o", out + "_bad"])
print("  exit code (expected 1): %d" % code)
assert code == 1

print("\n### unknown preset rejected")
try:
    main([base + "/", "--preset", "nonsense"])
    raise AssertionError("should have raised SystemExit")
except SystemExit as exc:
    print("  SystemExit code %s" % exc.code)

print("\n### --smart --plan-only")
out4 = out + "_plan"
shutil.rmtree(out4, ignore_errors=True)
code = main([base + "/", "--smart", "--plan-only", "-o", out4])
assert code == 0, "plan-only failed with %s" % code
assert not os.path.exists(os.path.join(out4, "report.json")), "plan-only must not crawl"

print("\n### --smart whole-site crawl with resume state")
out5 = out + "_smart"
shutil.rmtree(out5, ignore_errors=True)
code = main([base + "/", "--smart", "--max-pages", "4", "-o", out5])
assert code == 0
state = os.path.join(out5, "crawl-state.json")
print("  state file written: %s" % os.path.exists(state))
assert os.path.exists(state), "state file missing"

print("\n### resume with --no-resume discards state")
code = main([base + "/", "--smart", "--max-pages", "3", "-o", out5, "--no-resume"])
assert code == 0

print("\n### agent auto mode finds paginated pages on its own")
out6 = out + "_agent"
shutil.rmtree(out6, ignore_errors=True)
code = main([base + "/blog.html", "--auto", "--no-sitemap", "-o", out6])
assert code == 0, "agent crawl failed"
with open(os.path.join(out6, "report.json"), encoding="utf-8") as handle:
    payload = json.load(handle)
agent_summary = payload["summary"]["agent"]
print("  actions: %d" % agent_summary["actions_total"])
print("  by kind: %s" % agent_summary["actions_by_kind"])
print("  interaction mode: %s" % agent_summary["interactions"]["mode"])
paged = [p["url"] for p in payload["pages"] if "page=" in p["url"]]
print("  paginated urls reached: %d" % len(paged))
assert len(paged) > 0, "agent should have expanded pagination"

print("\n### agent disabled crawls fewer pages")
out7 = out + "_agentoff"
shutil.rmtree(out7, ignore_errors=True)
code = main([base + "/blog.html", "--no-sitemap", "--no-auto-actions", "-o", out7])
assert code == 0
with open(os.path.join(out7, "report.json"), encoding="utf-8") as handle:
    off_payload = json.load(handle)
print("  with agent: %d page(s), without: %d"
      % (payload["summary"]["pages_crawled"],
         off_payload["summary"]["pages_crawled"]))
assert off_payload["summary"]["agent"]["actions_total"] == 0

print("\n### decisions file records agent choices")
decisions = os.path.join(out6, "decisions.json")
print("  decisions file: %s" % os.path.exists(decisions))
assert os.path.exists(decisions)

print("\n### negative page budget rejected")
code = main([base + "/", "--max-pages", "-5"])
print("  exit code (expected 2): %d" % code)
assert code == 2

print("\n### anonymous crawl cannot see protected pages")
out8 = out + "_anon"
shutil.rmtree(out8, ignore_errors=True)
code = main([base + "/dashboard", "--no-sitemap", "-d", "127.0.0.1", "-o", out8])
assert code == 0
with open(os.path.join(out8, "report.json"), encoding="utf-8") as handle:
    anon = json.load(handle)
anon_titles = [p.get("title") for p in anon["pages"]]
print("  anonymous saw: %s" % anon_titles)
assert "Dashboard" not in anon_titles, "protected page leaked to anonymous crawl"

print("\n### --login-url signs in and reaches protected pages")
out9 = out + "_login"
shutil.rmtree(out9, ignore_errors=True)
os.environ["SMOKE_PW"] = OK_PASS
try:
    code = main([base + "/dashboard", "--no-sitemap", "-d", "127.0.0.1",
                 "--login-url", base + "/login", "-u", OK_USER,
                 "--password-env", "SMOKE_PW", "--require-login", "-o", out9])
    assert code == 0, "login crawl failed"
finally:
    del os.environ["SMOKE_PW"]

with open(os.path.join(out9, "report.json"), encoding="utf-8") as handle:
    logged = json.load(handle)
logged_titles = [p.get("title") for p in logged["pages"]]
print("  authenticated saw: %s" % logged_titles)
assert "Dashboard" in logged_titles, "login did not unlock the protected page"
assert "Reports" in logged_titles, "linked protected page not crawled"
assert logged["summary"]["auth"]["ok"] is True
session_path = os.path.join(out9, "session.json")
print("  session file: %s (mode %o)" % (os.path.exists(session_path),
                                         os.stat(session_path).st_mode & 0o777))
assert os.path.exists(session_path)
assert os.stat(session_path).st_mode & 0o777 == 0o600

print("\n### password never lands in the report")
blob = open(os.path.join(out9, "report.json"), encoding="utf-8").read()
print("  password present in report.json: %s" % (OK_PASS in blob))
assert OK_PASS not in blob

print("\n### --require-login aborts on bad credentials")
out10 = out + "_badlogin"
shutil.rmtree(out10, ignore_errors=True)
os.environ["SMOKE_BAD"] = "wrong-password"
try:
    code = main([base + "/dashboard", "--no-sitemap", "-d", "127.0.0.1",
                 "--login-url", base + "/login", "-u", OK_USER,
                 "--password-env", "SMOKE_BAD", "--require-login", "-o", out10])
finally:
    del os.environ["SMOKE_BAD"]
print("  exit code (expected 1): %d" % code)
assert code == 1

print("\n### saved session is reused without credentials")
out11 = out + "_reuse"
shutil.rmtree(out11, ignore_errors=True)
code = main([base + "/reports.html", "--no-sitemap", "-d", "127.0.0.1",
             "--session-file", session_path, "--require-login", "-o", out11])
assert code == 0, "session reuse failed"
with open(os.path.join(out11, "report.json"), encoding="utf-8") as handle:
    reused = json.load(handle)
print("  reused session, saw: %s" % [p.get("title") for p in reused["pages"]])
assert reused["summary"]["auth"]["ok"] is True

print("\n### login status in the HTML report")
report_html = open(os.path.join(out9, "report.html"), encoding="utf-8").read()
print("  html has login section: %s" % ("<h2>Login</h2>" in report_html))
assert "<h2>Login</h2>" in report_html
assert OK_PASS not in report_html

print("\n### no seeds rejected")
try:
    code = main(["--smart"])
    print("  main() returned %d (expected 2)" % code)
    assert code == 2
except SystemExit as exc:
    print("  argparse SystemExit code %s (expected 2)" % exc.code)
    assert exc.code == 2

server.shutdown()
server.server_close()

for d in (out, out2, out3, out + "_bad", out6, out7, out8, out9,
          out10, out11, out4, out5):
    shutil.rmtree(d, ignore_errors=True)
print("\nALL CLI SMOKE CHECKS PASSED")