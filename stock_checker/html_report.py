"""The report as a clickable page: every ticker links to a card with its
research note, ladder (filled rungs, when it last changed), review history,
position and a chart link.

Written next to the text reports as ratings_<time>.html, and copied to
reports/latest.html so one bookmark always opens the newest run. Lists every
name (like the _full report), new ones marked. Local only - it carries your
positions and notes.
"""

import html
import shutil
from datetime import datetime

from .constants import REPORTS_DIR

CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--mute:#6b6b70;--line:#e4e2dd;--card:#fff;
--acc:#2457c5;--up:#1b7f4b;--down:#b3261e;--warn:#9a6700;--chip:#f0eee9}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--mute:#9a9aa2;
--line:#2c2c31;--card:#1c1c20;--acc:#7aa2ff;--up:#5cc58c;--down:#ff8a80;--warn:#e3b341;--chip:#26262b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--mute);font-size:13px} .meta div{margin:2px 0}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{color:var(--mute);font-weight:500;font-size:12px}
td.tags{white-space:normal;color:var(--mute);font-size:12.5px}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
a.tk{font-weight:600}
.new{color:var(--warn);font-weight:700} .below{color:var(--down)} .above{color:var(--up)}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 6px;margin:0 3px 2px 0;font-size:12px;color:var(--fg)}
.wrap{overflow-x:auto}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:0 14px}
details[open]{padding-bottom:12px} summary{cursor:pointer;padding:10px 0;font-weight:600}
details:target{outline:2px solid var(--acc)}
.card p{margin:6px 0} .card .lbl{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:10px}
.filled{color:var(--mute);text-decoration:line-through}
.stale{color:var(--warn)}
"""

JS = """
function openHash(){var id=location.hash.slice(1);if(!id)return;var d=document.getElementById(id);
if(d&&d.tagName==='DETAILS'){d.open=true;d.scrollIntoView({block:'start'});}}
window.addEventListener('hashchange',openHash);window.addEventListener('load',openHash);
"""


def _e(s) -> str:
    return html.escape(str(s))


def _tk(t: str) -> str:
    return f'<a class="tk" href="#t-{_e(t)}">{_e(t)}</a>'


def _row(item: dict, side: str, tags: str) -> str:
    pct, tgt = item["pct_away"], item["target"]
    if side == "buy":
        status = (f'<span class="below">{abs(pct):.1f}% below</span> ${tgt:.2f}'
                  if item["triggered"] else f'{pct:.1f}% above ${tgt:.2f}')
    else:
        status = (f'<span class="above">{abs(pct):.1f}% above</span> ${tgt:.2f}'
                  if item["triggered"] else f'{pct:.1f}% below ${tgt:.2f}')
    rung = f'{item["rating"]}/{max(item.get("rungs", 0), item["rating"])}'
    if item.get("ladder", 1) > 1:
        rung += f' L{item["ladder"]}'
    if item.get("rung_zone") is not None:
        rung += f' Z{item["rung_zone"]}'
    new = '<span class="new">&#9733;</span>' if item.get("escalated") else ""
    auto = "$" if item.get("auto") else ""
    return (f'<tr><td>{new}</td><td>{_tk(item["ticker"])}{auto}</td><td>${item["price"]:.2f}</td>'
            f'<td>{status}</td><td>{"*" * item["rating"]} ({rung})</td><td class="tags">{tags}</td></tr>')


def _table(rows: list) -> str:
    head = "<tr><th></th><th>Ticker</th><th>Price</th><th>Target</th><th>Rung</th><th>Tags</th></tr>"
    return f'<div class="wrap"><table>{head}{"".join(rows)}</table></div>'


def _ladder_html(buys: list) -> str:
    if not buys:
        return "<p>No buy rungs.</p>"
    cells = []
    for b in sorted(buys, key=lambda b: b["rating"]):
        txt = f'{"*" * b["rating"]} ${b["price"]:g}'
        if b.get("filled"):
            cells.append(f'<span class="chip filled" title="filled {b["filled"]}">{txt}</span>')
        else:
            cells.append(f'<span class="chip">{txt}</span>')
    return "<p>" + "".join(cells) + "</p>"


def _what_happened(h: dict) -> str:
    """A review note in words: "b 147.6 3" -> "bought $148 at $49.20 - rung 1"."""
    if h.get("bought"):
        b = h["bought"]
        return f'bought ${b["dollars"]:,.0f} at ${b["price"]:.2f} - rung {b["rung"]}'
    mark = str(h.get("mark", ""))
    return {"s": "skipped - moved up one rung", "radar": "radar - old floor made rung 1",
            "r": "rejected - muted"}.get(mark, mark)


def _card(t: str, entry: dict, note: dict | None, pos, stale) -> str:
    bits = []
    if pos:
        qty, basis = pos
        bits.append(f'<p><b>Held:</b> {qty:g} @ ${basis:.2f} (${qty * basis:,.0f})</p>')
    if note:
        st = ' <span class="stale">(stale - refresh)</span>' if stale(note) else ""
        bits.append(f'<div class="lbl">Research - {_e(note.get("updated", "?"))}{st}</div>')
        if note.get("what"):
            bits.append(f'<p>{_e(note["what"])}</p>')
        if note.get("ai"):
            bits.append(f'<p><b>AI {_e(note["ai"])}</b> - {_e(note.get("ai_why", ""))}</p>')
        if note.get("outlook"):
            bits.append(f'<p><b>Outlook:</b> {_e(note["outlook"])}</p>')
    else:
        bits.append('<p class="stale">No research note yet - ask Claude about it.</p>')

    src = "auto ($)" if entry.get("source") == "auto" else "hand-reviewed"
    lad = f' &middot; ladder L{entry["ladder"]}' if entry.get("ladder", 1) > 1 else ""
    bits.append(f'<div class="lbl">Buy ladder - updated {_e(entry.get("updated", "?"))}, {src}{lad}</div>')
    bits.append(_ladder_html(entry.get("buys", [])))
    if entry.get("sells"):
        bits.append(f'<div class="lbl">Sell ladder - {_e(entry.get("sells_updated", "?"))}</div>')
        bits.append(_ladder_html(entry["sells"]))
    hist = entry.get("reviews", []) + [
        {"date": b.get("rolled"), "mark": f'rolled L{b.get("ladder")} (broke at ${b.get("price")})'}
        for b in entry.get("broken", [])]
    if hist:
        hist.sort(key=lambda h: h.get("date") or "")
        bits.append('<div class="lbl">History</div>')
        bits.append("".join(f'<p>{_e(h.get("date"))} - {_e(_what_happened(h))}</p>' for h in hist))
    chart = f"https://www.tradingview.com/chart/?symbol={_e(t)}"
    bits.append(f'<p><a href="{chart}" target="_blank" rel="noopener">Open chart &rarr;</a>'
                f' &middot; <a href="#top">back to top</a></p>')
    title = f'{_e(t)}' + (f' &middot; AI {_e(note["ai"])}' if note and note.get("ai") else "")
    return f'<details id="t-{_e(t)}" class="card"><summary>{title}</summary>{"".join(bits)}</details>'


def write_html_report(header: list, sections: list, ratings: dict, notes: dict,
                      holdings: dict, tag_fn, stale_fn, timestamp: str):
    """header: lines of text. sections: [(title, side, items)] - side is
    "buy", "sell" or "zone". tag_fn(item) -> tag string."""
    seen, body = [], []
    for title, side, items in sections:
        if not items:
            continue
        body.append(f"<h2>{_e(title)}</h2>")
        rows = []
        for it in items:
            if it["ticker"] not in seen:
                seen.append(it["ticker"])
            tags = _e(tag_fn(it))
            if side == "zone":
                rows.append(f'<tr><td><span class="new">&#9733;</span></td><td>{_tk(it["ticker"])}</td>'
                            f'<td>${it["price"]:.2f}</td><td>Zone {it["was"]} &rarr; {it["zone"]}</td>'
                            f'<td>{it["off_high"]:.0f}% off high</td><td class="tags">{tags}</td></tr>')
            else:
                rows.append(_row(it, side, tags))
        body.append(_table(rows))

    positions = holdings.get("positions", {})
    cards = [_card(t, ratings.get(t, {}), notes.get(t), positions.get(t), stale_fn)
             for t in sorted(seen)]
    page = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Ratings {timestamp}</title><style>{CSS}</style></head><body><main id="top">'
            f'<h1>Stock Ratings</h1><div class="meta">'
            + "".join(f"<div>{_e(h)}</div>" for h in header)
            + "</div>" + "".join(body)
            + f'<h2>Tickers ({len(cards)})</h2>' + "".join(cards)
            + f"</main><script>{JS}</script></body></html>")
    path = REPORTS_DIR / f"ratings_{timestamp}.html"
    path.write_text(page, encoding="utf-8")
    shutil.copy(path, REPORTS_DIR / "latest.html")
    return path
