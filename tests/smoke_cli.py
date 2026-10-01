from __future__ import annotations

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
from test_crawler import Handler

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_output")
shutil.rmtree(out, ignore_errors=True)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
Handler.port = server.server_address[1]
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

server.shutdown()
server.server_close()

for d in (out, out2, out3, out + "_bad"):
    shutil.rmtree(d, ignore_errors=True)
print("\nALL CLI SMOKE CHECKS PASSED")