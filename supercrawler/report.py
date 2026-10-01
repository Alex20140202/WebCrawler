from __future__ import annotations

import csv
import html
import json
import os
from typing import Dict, Optional, Sequence

CSV_COLUMNS = [
    "url", "depth", "status", "content_type", "title", "title_length",
    "meta_description", "lang", "word_count", "h1_count", "image_count",
    "images_missing_alt", "internal_links", "external_links", "emails",
    "has_viewport", "has_og", "has_twitter_card", "needs_login", "findings",
    "fetch_ms", "error",
]


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_json(path: str, payload: Dict) -> str:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def write_jsonl(path: str, pages: Sequence[Dict]) -> str:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as handle:
        for page in pages:
            handle.write(json.dumps(page, ensure_ascii=False) + "\n")
    return path


def _flat_row(page: Dict) -> Dict:
    return {
        "url": page.get("url", ""),
        "depth": page.get("depth", 0),
        "status": page.get("status", 0),
        "content_type": page.get("content_type", ""),
        "title": page.get("title", ""),
        "title_length": page.get("title_length", 0),
        "meta_description": page.get("meta_description", ""),
        "lang": page.get("lang") or "",
        "word_count": page.get("word_count", 0),
        "h1_count": page.get("h1_count", 0),
        "image_count": page.get("image_count", 0),
        "images_missing_alt": page.get("images_missing_alt", 0),
        "internal_links": len(page.get("internal_links", []) or []),
        "external_links": len(page.get("external_links", []) or []),
        "emails": ";".join(page.get("emails", []) or []),
        "has_viewport": page.get("has_viewport", False),
        "has_og": page.get("has_og", False),
        "has_twitter_card": page.get("has_twitter_card", False),
        "needs_login": page.get("needs_login", False),
        "findings": ";".join(sorted({f.get("kind", "") for f
                                     in (page.get("findings") or [])})),
        "fetch_ms": round((page.get("fetch_ms") or 0.0), 1),
        "error": page.get("error") or "",
    }


def write_csv(path: str, pages: Sequence[Dict]) -> str:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for page in pages:
            writer.writerow(_flat_row(page))
    return path


def _bar(value: int, maximum: int, width: int = 24) -> str:
    if maximum <= 0:
        return ""
    filled = int(round(width * value / float(maximum)))
    return "█" * filled + "·" * (width - filled)


def write_html(path: str, pages: Sequence[Dict], summary: Optional[Dict] = None) -> str:
    _ensure_parent(path)
    summary = summary or {}
    esc = html.escape

    depths = [p.get("depth", 0) for p in pages]
    depth_counts = {}
    for depth in depths:
        depth_counts[depth] = depth_counts.get(depth, 0) + 1
    max_depth_count = max(depth_counts.values()) if depth_counts else 0

    domain_counts = {}
    for page in pages:
        key = page.get("host") or "-"
        domain_counts[key] = domain_counts.get(key, 0) + 1
    top_domains = sorted(domain_counts.items(), key=lambda kv: -kv[1])[:10]

    coverage = summary.get("coverage") or {}
    cards = [
        ("Pages crawled", summary.get("pages_crawled", len(pages))),
        ("Success", summary.get("successful", 0)),
        ("Failed", summary.get("failed", 0)),
        ("Duplicates", summary.get("duplicates", 0)),
        ("Skipped", summary.get("skipped", 0)),
        ("Total words", sum(p.get("word_count", 0) or 0 for p in pages)),
        ("Emails found", len(summary.get("all_emails", []))),
        ("Duration", "%0.1fs" % summary.get("elapsed", 0.0)),
    ]
    if coverage:
        cards.insert(1, ("Coverage", "%s%%" % coverage.get("percent", 0)))
    if summary.get("pending"):
        cards.append(("Still queued", summary["pending"]))
    if summary.get("js_rendered_pages"):
        cards.append(("JS-rendered", summary["js_rendered_pages"]))
    card_html = "".join(
        '<div class="card"><div class="label">%s</div><div class="value">%s</div></div>'
        % (esc(str(label)), esc(str(value)))
        for label, value in cards
    )

    coverage_html = ""
    if coverage:
        known = coverage.get("known_urls", 0)
        got = coverage.get("crawled", 0)
        coverage_html = (
            '<h2>Site coverage</h2><div class="panel">'
            '<div class="row"><span class="dom">sitemap URLs</span>'
            '<span class="bar">%s</span><span class="num">%d / %d</span></div>'
            '<p class="muted">%d URL(s) discovered from sitemaps were not reached in '
            'this run. Re-run with the same --state-file to continue.</p></div>'
            % (_bar(got, known, 48), got, known, coverage.get("remaining", 0))
        )

    domain_html = "".join(
        '<div class="row"><span class="dom">%s</span><span class="bar">%s</span>'
        '<span class="num">%d</span></div>'
        % (esc(dom), _bar(count, top_domains[0][1] if top_domains else 1), count)
        for dom, count in top_domains
    ) or '<p class="muted">No data.</p>'

    depth_html = "".join(
        '<div class="row"><span class="dom">depth %d</span><span class="bar">%s</span>'
        '<span class="num">%d</span></div>'
        % (depth, _bar(count, max_depth_count), count)
        for depth, count in sorted(depth_counts.items())
    ) or '<p class="muted">No data.</p>'

    rows = []
    for page in pages:
        status = page.get("status", 0) or 0
        if page.get("error"):
            badge = "err"
        elif 200 <= status < 300:
            badge = "ok"
        else:
            badge = "warn"
        title = page.get("title") or page.get("url", "")
        rows.append(
            '<tr>'
            '<td><a href="%s" target="_blank" rel="noopener noreferrer">%s</a></td>'
            '<td>%d</td><td><span class="badge %s">%s</span></td>'
            '<td class="title">%s</td><td>%d</td><td>%s</td><td>%d</td>'
            '</tr>'
            % (
                esc(page.get("url", "")), esc(_short(page.get("url", ""), 70)),
                page.get("depth", 0), badge, status,
                esc(_short(title, 80)),
                page.get("word_count", 0) or 0,
                esc(_short(page.get("meta_description", "") or "", 70)),
                page.get("internal_links_count", 0),
            )
        )
    rows_html = "".join(rows) or '<tr><td colspan="7" class="muted">No pages crawled.</td></tr>'

    emails = summary.get("all_emails", [])
    email_html = ", ".join(
        '<a href="mailto:%s">%s</a>' % (esc(e), esc(e)) for e in emails[:200]
    ) or '<p class="muted">None found.</p>'

    agent_info = summary.get("agent") or {}
    actions = agent_info.get("actions_by_kind") or {}
    interaction = agent_info.get("interactions") or {}
    agent_html = ""
    if actions or interaction:
        rows_actions = "".join(
            '<div class="row"><span class="dom">%s</span>'
            '<span class="bar">%s</span><span class="num">%d</span></div>'
            % (esc(kind.replace("_", " ")), _bar(count, max(actions.values())), count)
            for kind, count in sorted(actions.items(), key=lambda kv: -kv[1])
        ) or '<p class="muted">The crawler took no extra actions.</p>'
        agent_html = (
            '<h2>Crawler actions</h2><div class="panel">%s'
            '<p class="muted">mode: %s · questions asked: %d · skipped: %d'
            ' · topics: %s</p></div>'
            % (rows_actions, esc(interaction.get("mode", "?")),
               interaction.get("questions_asked", 0),
               interaction.get("questions_skipped", 0),
               esc(", ".join(agent_info.get("topics_consulted", [])) or "none"))
        )

    walls = summary.get("login_walls") or []
    wall_html = ""
    if walls:
        wall_html = (
            '<h2>Pages needing a login (%d)</h2><div class="panel">'
            '<p class="muted">Not fetched. This crawler does not sign in.</p>%s</div>'
            % (len(walls), "".join(
                '<div><a href="%s" target="_blank" rel="noopener noreferrer">%s</a></div>'
                % (esc(w), esc(w)) for w in walls[:50]))
        )

    document = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Crawl report</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; padding:2rem; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#0f1115; color:#e6e8ee; }
h1 { margin:0 0 .25rem; font-size:1.5rem; }
.sub { color:#8b93a7; margin-bottom:1.5rem; }
.cards { display:flex; flex-wrap:wrap; gap:.75rem; margin-bottom:2rem; }
.card { background:#171a21; border:1px solid #242936; border-radius:10px; padding:.85rem 1.1rem; min-width:130px; }
.card .label { color:#8b93a7; font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; }
.card .value { font-size:1.35rem; font-weight:600; margin-top:.2rem; }
h2 { font-size:1.05rem; margin:2rem 0 .75rem; }
.panel { background:#171a21; border:1px solid #242936; border-radius:10px; padding:1rem 1.1rem; margin-bottom:1rem; }
.row { display:flex; align-items:center; gap:.75rem; padding:.2rem 0; }
.dom { width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#c3c9d6; }
.bar { font-family:ui-monospace,Menlo,monospace; color:#4f8cff; letter-spacing:-1px; }
.num { color:#8b93a7; }
table { width:100%%; border-collapse:collapse; font-size:13px; }
th { text-align:left; color:#8b93a7; font-weight:600; border-bottom:1px solid #242936; padding:.5rem; }
td { padding:.5rem; border-bottom:1px solid #1c212b; vertical-align:top; }
td.title { color:#c3c9d6; }
a { color:#4f8cff; text-decoration:none; }
a:hover { text-decoration:underline; }
.badge { padding:.1rem .45rem; border-radius:5px; font-family:ui-monospace,Menlo,monospace; font-size:.8rem; }
.badge.ok { background:#12331f; color:#4ade80; }
.badge.warn { background:#33290f; color:#fbbf24; }
.badge.err { background:#3a1a1a; color:#f87171; }
.muted { color:#8b93a7; }
.emailbox { line-height:2; font-size:13px; }
</style></head><body>
<h1>Crawl report</h1>
<div class="sub">%(started)s · %(seeds)s%(resumed)s</div>
<div class="cards">%(cards)s</div>
%(coverage)s
<h2>Top hosts</h2><div class="panel">%(domains)s</div>
<h2>Pages by depth</h2><div class="panel">%(depths)s</div>
<h2>Emails (%(email_count)d)</h2><div class="panel emailbox">%(emails)s</div>
%(agent)s%(walls)s
<h2>Pages (%(page_count)d)</h2>
<table><thead><tr><th>URL</th><th>Depth</th><th>Status</th><th>Title</th>
<th>Words</th><th>Meta</th><th>Links</th></tr></thead><tbody>%(rows)s</tbody></table>
</body></html>
""" % {
        "started": esc(summary.get("started_at", "")),
        "seeds": esc(", ".join(summary.get("seeds", []))),
        "cards": card_html,
        "coverage": coverage_html,
        "resumed": " · resumed" if summary.get("resumed") else "",
        "domains": domain_html,
        "depths": depth_html,
        "emails": email_html,
        "agent": agent_html,
        "walls": wall_html,
        "email_count": len(emails),
        "page_count": len(pages),
        "rows": rows_html,
    }

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document)
    return path


def _short(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_reports(
    pages: Sequence[Dict],
    summary: Dict,
    output_dir: str = "output",
    html_path: Optional[str] = None,
    csv_path: Optional[str] = None,
) -> Dict[str, str]:
    written = {}
    written["json"] = write_json(os.path.join(output_dir, "report.json"),
                                 {"summary": summary, "pages": list(pages)})
    written["jsonl"] = write_jsonl(os.path.join(output_dir, "pages.jsonl"), pages)
    written["csv"] = write_csv(csv_path or os.path.join(output_dir, "report.csv"), pages)
    written["html"] = write_html(html_path or os.path.join(output_dir, "report.html"),
                                 pages, summary)
    return written