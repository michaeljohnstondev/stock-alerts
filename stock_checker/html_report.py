"""The report as a clickable page: every ticker links to a card with its
research note, ladder (filled rungs, when it last changed), review history,
position and a chart link.

Written next to the text reports as ratings_<time>.html, and copied to
reports/latest.html so one bookmark always opens the newest run. Lists every
name (like the _full report), new ones marked. Local only - it carries your
positions and notes.
"""

import html
import json
import shutil

from .charts import chart_linker
from .constants import REPORTS_DIR
from .snapshot import describe, now_row

_PRICES = {}  # ticker -> latest price on the page, for "since the buy"
_HELD = {}    # ticker -> shares you own, for the Owned column
_PE = {}      # ticker -> (forward P/E, sector median forward P/E)


def _load_sector_pe():
    """Forward P/E vs the median of its sector across your own tickers -
    a bank at 11 and software at 11 mean different things."""
    import statistics
    from .constants import FUNDAMENTAL_CACHE_FILE, SECTOR_CACHE_FILE
    try:
        fund = json.loads(FUNDAMENTAL_CACHE_FILE.read_text(encoding="utf-8"))
        sect = json.loads(SECTOR_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    by_sector = {}
    for tk, f in fund.items():
        pe, s = f.get("forward_pe"), (sect.get(tk) or {}).get("sector")
        if pe and pe > 0 and s:
            by_sector.setdefault(s, []).append(pe)
    med = {s: statistics.median(v) for s, v in by_sector.items() if len(v) >= 5}
    _PE.clear()
    for tk, f in fund.items():
        pe, s = f.get("forward_pe"), (sect.get(tk) or {}).get("sector")
        if pe and pe > 0 and s in med:
            _PE[tk] = (pe, med[s], s)


def _sector_cell(ticker: str) -> str:
    v = _PE.get(ticker)
    if not v:
        return _td("", "")
    pe, med, s = v
    x = pe / med
    cls = "g-a" if x < 0.8 else "g-d" if x > 1.25 else ""
    return (f'<td data-v="{x:.3f}" class="{cls}" title="Fwd P/E {pe:.1f} vs {_e(s)} median {med:.1f}">'
            f'{x:.2f}&times;</td>')


def _arrange(first: list, mid: list, common: list, ticker: str) -> list:
    """Column order: Owned, Ticker, Price, <section columns>, Zone, AI, Rung,
    DP, Earn, Val, vs Sector, Details. first = [owned, rung, ticker, price],
    common = [zone, ai, dp, earn, val, details]."""
    owned, rung, tick, price = first
    zone, ai, dp, earn, val, details = common
    return [owned, tick, price] + mid + [zone, ai, rung, dp, earn, val,
                                         _sector_cell(ticker), details]

# Set per run from config.json "chart_site" (see charts.py)
_chart = lambda t: f"https://www.tradingview.com/chart/?symbol={t}"

CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--mute:#6b6b70;--line:#e4e2dd;--card:#fff;
--acc:#2457c5;--up:#1b7f4b;--down:#b3261e;--warn:#9a6700;--chip:#f0eee9}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--mute:#9a9aa2;
--line:#2c2c31;--card:#1c1c20;--acc:#7aa2ff;--up:#5cc58c;--down:#ff8a80;--warn:#e3b341;--chip:#26262b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1600px;margin:0 auto;padding:20px 24px 80px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--mute);font-size:13px} .meta div{margin:2px 0}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:7px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{color:var(--mute);font-weight:500;font-size:12px;cursor:pointer;user-select:none}
th:hover{color:var(--fg)} th.asc::after{content:" ▲"} th.desc::after{content:" ▼"}
td.num{text-align:right} th.num{text-align:right}
.g-a{color:var(--up);font-weight:600} .g-d{color:var(--down);font-weight:600}
.dp-lo{color:var(--down);font-weight:600} .dp-hi{color:var(--up);font-weight:600}
.soon{color:var(--warn);font-weight:600}
td.tags{white-space:normal;color:var(--mute);font-size:12.5px}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
a.tk{font-weight:600}
.new{color:var(--warn);font-weight:700} .below{color:var(--down)} .above{color:var(--up)}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 6px;margin:0 3px 2px 0;font-size:12px;color:var(--fg)}
.wrap{overflow-x:auto}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:0 14px}
details[open]{padding-bottom:12px} summary{cursor:pointer;padding:10px 0;font-weight:600}
details:target{outline:2px solid var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 16px 12px} .card p{margin:6px 0} .card .lbl{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:10px}
.filled{color:var(--mute);text-decoration:line-through}
.stale{color:var(--warn)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:18px 0 4px}
.tabs button{font:inherit;font-size:13px;background:var(--chip);color:var(--fg);border:1px solid var(--line);
border-radius:999px;padding:5px 12px;cursor:pointer}
.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.tabs .n{opacity:.7;margin-left:4px}
"""

JS = """
function openHash(){var id=location.hash.slice(1);if(!id)return;var d=document.getElementById(id);
if(d&&d.tagName==='DETAILS'){d.open=true;d.scrollIntoView({block:'start'});}}
window.addEventListener('hashchange',openHash);window.addEventListener('load',openHash);
function showTab(btn){var f=btn.dataset.f;
[].forEach.call(document.querySelectorAll('.tabs button'),function(b){b.classList.toggle('on',b===btn)});
[].forEach.call(document.querySelectorAll('section'),function(s){var k=s.dataset.kind,vis=0;
[].forEach.call(s.querySelectorAll('tr[data-k]'),function(r){
var ok=(f==='all'&&k!=='owned')||f===k||(f.charAt(0)==='r'&&k==='buy'&&r.dataset.r===f.slice(1));
r.style.display=ok?'':'none';if(ok)vis++;});s.style.display=vis?'':'none';});}
document.addEventListener('click',function(e){var b=e.target.closest('.tabs button');if(b)showTab(b);});
document.addEventListener('click',function(e){var th=e.target.closest('th');if(!th)return;
var table=th.closest('table'),idx=[].indexOf.call(th.parentNode.children,th);
var dir=th.classList.contains('asc')?-1:1;
[].forEach.call(th.parentNode.children,function(h){h.classList.remove('asc','desc')});
th.classList.add(dir===1?'asc':'desc');
var rows=[].slice.call(table.querySelectorAll('tr')).slice(1);
rows.sort(function(a,b){var x=a.children[idx].dataset.v,y=b.children[idx].dataset.v;
var nx=parseFloat(x),ny=parseFloat(y);
if(!isNaN(nx)&&!isNaN(ny))return (nx-ny)*dir;
if(x===''||x===undefined)return 1; if(y===''||y===undefined)return -1;
return x.localeCompare(y)*dir;});
rows.forEach(function(r){table.appendChild(r)});});
"""


def _e(s) -> str:
    return html.escape(str(s))


def _tk(t: str) -> str:
    # each ticker's card is its own page, opened in a new tab
    return f'<a class="tk" href="cards/{_e(t)}.html" target="_blank">{_e(t)}</a>'


# AI grade -> a number that sorts: A+ highest, F lowest
_GRADE = {"A": 12, "B": 9, "C": 6, "D": 3, "F": 0}


def _grade_v(g) -> str:
    if not g:
        return ""
    base = _GRADE.get(g[0].upper(), 0)
    return str(base + (1 if g.endswith("+") else -1 if g.endswith("-") else 0))


def _td(text, v="", cls="") -> str:
    c = f' class="{cls}"' if cls else ""
    return f'<td data-v="{_e(v)}"{c}>{text}</td>'


def _common(item: dict) -> list:
    """Zone, AI, DP, Earnings, Val and the valuation detail - shared columns."""
    zone = item.get("zone")
    ai = item.get("ai") or ""
    ai_cls = "g-a" if ai[:1] in ("A", "B") else "g-d" if ai[:1] in "DF" and ai else ""
    dp = item.get("dp") or {}
    sc = dp.get("score")
    dp_txt = f'{sc} ({dp["trend"]})' if sc and dp.get("trend") else (str(sc) if sc else "")
    dp_cls = "dp-lo" if sc and sc <= 3 else "dp-hi" if sc and sc >= 7 else ""
    earn = item.get("earnings") or {}
    du, ds = earn.get("days_until"), earn.get("days_since")
    earn_txt = (f"+{du}d" if du is not None else "") + (f" -{ds}d" if ds is not None else "")
    earn_cls = "soon" if du is not None and du <= 5 else ""
    val = item.get("value") or {}
    grade = val.get("grade") if val.get("grade") not in (None, "N/A") else ""
    val_txt = f'{grade} ({val.get("score", 0)})' if grade else ""
    detail = " | ".join(d for k in ("valuation", "profitability", "health", "growth")
                        for d in [val.get("details", {}).get(k, "")] if d and d != "no data")
    return [_td("" if zone is None else zone, "" if zone is None else zone, "num"),
            _td(_e(ai), _grade_v(ai), ai_cls),
            _td(_e(dp_txt), sc or "", dp_cls),
            _td(_e(earn_txt.strip()), du if du is not None else 999, earn_cls),
            _td(_e(val_txt), val.get("score", "") if grade else "",
                "g-a" if grade[:1] in ("A", "B") else "g-d" if grade[:1] in ("D", "F") else ""),
            f'<td class="tags">{_e(detail)}</td>']


def _first(item: dict) -> list:
    # the left column is the rung, in stars: 4 stars = rung 4, 5 = watch price
    r = item.get("rating")
    stars = f'<span class="new">{"&#9733;" * r}</span>' if r else ""
    auto = "$" if item.get("auto") else ""
    qty = _HELD.get(item["ticker"])
    owned = _td(f'<span class="above">$ {qty:g}</span>', qty) if qty else _td("", 0)
    return [owned, _td(stars, r or 0), _td(_tk(item["ticker"]) + auto, item["ticker"]),
            _td(f'<a href="{_e(_chart(item["ticker"]))}" target="_blank" rel="noopener">'
                f'${item["price"]:.2f}</a>', item["price"], "num")]


def _row(item: dict, side: str) -> str:
    pct, tgt = item["pct_away"], item["target"]
    # signed so sorting reads naturally: through the rung is negative
    dist = -abs(pct) if item["triggered"] else pct
    if side == "buy":
        status = (f'<span class="below">{abs(pct):.1f}% below</span> ${tgt:.2f}'
                  if item["triggered"] else f'{pct:.1f}% above ${tgt:.2f}')
    else:
        status = (f'<span class="above">{abs(pct):.1f}% above</span> ${tgt:.2f}'
                  if item["triggered"] else f'{pct:.1f}% below ${tgt:.2f}')
    total = max(item.get("rungs", 0), item["rating"])
    rung = "watch price" if item["rating"] == 5 else f'{total} rungs'
    extra = []
    if item.get("ladder", 1) > 1:
        extra.append(f'ladder {item["ladder"]}')
    if item.get("rung_zone") is not None:
        extra.append(f'in zone {item["rung_zone"]}')
    if extra:
        rung += f' <span class="tags">({", ".join(extra)})</span>'
    cells = _arrange(_first(item), [_td(status, dist), _td(rung, total)], _common(item),
                     item["ticker"])
    return f'<tr data-r="{item["rating"]}">' + "".join(cells) + "</tr>"


def _zone_row(item: dict) -> str:
    # The rung column for a zone move: where the stock sits on its own
    # ladder - solid stars = through that rung, faded = within 5% of it,
    # a dash = no rung near (price above them all, or the ladder is spent).
    r, how = item.get("ladder_rung"), item.get("ladder_how")
    if r:
        cls = "new" if how == "hit" else "tags"
        tip = "through rung" if how == "hit" else "approaching rung"
        first = _td(f'<span class="{cls}" title="{tip} {r}">{"&#9733;" * r}</span>', r)
    else:
        first = _td('<span class="tags" title="no rung within 5%">&ndash;</span>', 0)
    base = _first(item)
    cells = _arrange([base[0], first, base[2], base[3]], [
        _td(f'{item["was"]} &rarr; {item["zone"]}', item["zone"]),
        _td(f'{item["off_high"]:.0f}%', item["off_high"], "num"),
    ], _common(item), item["ticker"])
    return "<tr>" + "".join(cells) + "</tr>"


def _owned_row(item: dict) -> str:
    pl = item["pl"]
    cls = "above" if pl >= 0 else "below"
    ns = item.get("next_sell")
    sell = (f'${ns:g} <span class="tags">(+{(ns / item["price"] - 1) * 100:.1f}%)</span>'
            if ns else '<span class="tags">none above</span>')
    cells = _arrange(_first(item), [
        _td(f'{item["qty"]:g} @ ${item["basis"]:.2f}', item["value_usd"]),
        _td(f'<span class="{cls}">{pl:+.1f}%</span>', pl, "num"),
        _td(sell, (ns / item["price"] - 1) * 100 if ns else 999),
    ], _common(item), item["ticker"])
    return "<tr>" + "".join(cells) + "</tr>"


COLS = ["Zone", "AI", "DP", "Earn", "Val", "Details"]


def _table(rows: list, side: str) -> str:
    mid = {"zone": ["Move", "Off high"],
           "owned": ["Position", "P/L", "Next sell"]}.get(side, ["Target", "Ladder"])
    cols = ["Owned", "Ticker", "Price"] + mid + ["Zone", "AI", "Rung", "DP", "Earn", "Val",
                                                    "vs Sector", "Details"]
    head = "".join(f"<th>{h}</th>" for h in cols)
    return f'<div class="wrap"><table><tr>{head}</tr>{"".join(rows)}</table></div>'


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
        line = f'bought ${b["dollars"]:,.0f} at ${b["price"]:.2f} - rung {b["rung"]}'
        if h.get("snapshot"):
            line += f' - snapshot at {h["snapshot"]["at"][11:]} below'
            now = _PRICES.get(h.get("ticker", ""))
            if now and b.get("price"):
                line += f' || since: {(now / b["price"] - 1) * 100:+.1f}%'
        return line
    if h.get("sold"):
        x = h["sold"]
        pl = f' ({x["pl_pct"]:+.1f}% vs ${x["basis"]:.2f} cost)' if x.get("pl_pct") is not None else ""
        return f'sold {x["shares"]:g} at ${x["price"]:.2f}{pl}' + (" - whole position" if x.get("all") else "")
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
        for h in hist:
            h["ticker"] = t
        bits.append('<div class="lbl">History</div>')
        for h in hist:
            bits.append(f'<p>{_e(h.get("date"))} - {_e(_what_happened(h))}</p>')
            row = (h.get("snapshot") or {}).get("row")
            if row:
                # the line at the moment of the buy, then the same line now
                when = "At the sale" if h.get("sold") else "At the buy"
                bits.append(f'<div class="lbl">{when}</div>' + _table([_row(row, "buy")], "buy"))
                try:
                    cur = now_row(t, row)
                except Exception:
                    cur = None
                if cur:
                    bits.append('<div class="lbl">Now</div>' + _table([_row(cur, "buy")], "buy"))
    chart = _e(_chart(t))
    bits.append(f'<p><a href="{chart}" target="_blank" rel="noopener">Open chart &rarr;</a>'
                '</p>')
    title = f'{_e(t)}' + (f' &middot; AI {_e(note["ai"])}' if note and note.get("ai") else "")
    return f'<h1>{title}</h1><div class="card">{"".join(bits)}</div>'


def write_html_report(header: list, sections: list, ratings: dict, notes: dict,
                      holdings: dict, stale_fn, timestamp: str):
    """header: lines of text. sections: [(title, kind, items)] - kind is
    "cheaper", "recovering", "buy" or "sell". Click any column header to
    sort that section; tabs filter by kind or by buy rung."""
    # Saved beside the page so a layout change can be re-rendered without
    # a new scan: python -m stock_checker.html_report
    (REPORTS_DIR / "latest.json").write_text(json.dumps(
        {"header": header, "sections": sections, "timestamp": timestamp},
        default=str), encoding="utf-8")

    global _chart
    _chart = chart_linker(sorted({it["ticker"] for _, _, items in sections for it in items}))

    # AI grade straight from the notes, so a note written after the scan
    # shows on the next re-render
    for _, _, items in sections:
        for it in items:
            it["ai"] = (notes.get(it["ticker"]) or {}).get("ai")

    # each ticker's rung, for the zone rows: deepest hit, else nearest approaching
    hit, near = {}, {}
    for _, kind, items in sections:
        for it in items if kind == "buy" else []:
            book = hit if it["triggered"] else near
            if it["rating"] > book.get(it["ticker"], 0):
                book[it["ticker"]] = it["rating"]
    for _, kind, items in sections:
        if kind in ("cheaper", "recovering"):
            for it in items:
                tk = it["ticker"]
                it["ladder_rung"] = hit.get(tk) or near.get(tk)
                it["ladder_how"] = "hit" if tk in hit else "near" if tk in near else None

    for _, _, items in sections:
        for it in items:
            _PRICES[it["ticker"]] = it["price"]
    _load_sector_pe()
    _HELD.clear()
    _HELD.update({tk: v[0] for tk, v in holdings.get("positions", {}).items()})

    seen, body, counts = [], [], {}
    by_ticker = {}  # ticker -> [(section title, kind, row)] for its card
    for title, kind, items in sections:
        if not items:
            continue
        rows = []
        for it in items:
            if it["ticker"] not in seen:
                seen.append(it["ticker"])
            zone = kind in ("cheaper", "recovering")
            row = (_zone_row(it) if zone else _owned_row(it) if kind == "owned"
                   else _row(it, kind))
            rows.append(row.replace("<tr", f'<tr data-k="{kind}"', 1))
            by_ticker.setdefault(it["ticker"], []).append((title, "zone" if zone else kind, row))
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "buy":
                counts[f"r{it['rating']}"] = counts.get(f"r{it['rating']}", 0) + 1
        body.append(f'<section data-kind="{kind}"><h2>{_e(title)}</h2>'
                    f'{_table(rows, "zone" if zone else kind)}</section>')
        if kind == "owned":
            # your positions: card links too, but not counted in "All"
            counts["owned"] = len(items)

    tabs = [("all", "All"), ("cheaper", "Cheaper"), ("recovering", "Climbing")] + \
           [(f"r{n}", "&#9733;" * n) for n in (5, 4, 3, 2, 1)] + [("sell", "Sells"), ("owned", "Owned")]
    total = sum(v for k, v in counts.items() if not k.startswith("r") and k != "owned")
    tab_html = '<div class="tabs">' + "".join(
        f'<button data-f="{f}"{" class=on" if f == "all" else ""}>{label}'
        f'<span class="n">{total if f == "all" else counts.get(f, 0)}</span></button>'
        for f, label in tabs if f == "all" or counts.get(f)) + "</div>"

    positions = holdings.get("positions", {})
    # one page per ticker in reports/cards/, rewritten every run
    card_dir = REPORTS_DIR / "cards"
    card_dir.mkdir(exist_ok=True)
    for t in seen:
        card = _card(t, ratings.get(t, {}), notes.get(t), positions.get(t), stale_fn)
        # the ticker's whole report rows, so the card has everything at once
        rows_html = "".join(f'<div class="lbl">{_e(title)}</div>{_table([row], kind)}'
                            for title, kind, row in by_ticker.get(t, []))
        # the card already lives in cards/, so its own ticker link is a sibling
        rows_html = rows_html.replace('href="cards/', 'href="')
        card = card.replace('<div class="card">', f'<div class="card">{rows_html}', 1)
        (card_dir / f"{t}.html").write_text(
            f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_e(t)}</title><style>{CSS}</style></head><body><main>'
            f'{card}<p class="meta">as of run {_e(timestamp)}</p></main></body></html>',
            encoding="utf-8")
    page = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Ratings {timestamp}</title><style>{CSS}</style></head><body><main id="top">'
            f'<h1>Stock Ratings</h1><div class="meta">'
            + "".join(f"<div>{_e(h)}</div>" for h in header)
            + '<div>&#9733; = rung (&#9733;&#9733;&#9733;&#9733; = rung 4, &#9733;&#9733;&#9733;&#9733;&#9733; = watch price). Click a ticker for its card,'
              ' a column header to sort (again to reverse).</div>'
            + "</div>" + tab_html + "".join(body)
            + f"</main><script>{JS}</script></body></html>")
    path = REPORTS_DIR / f"ratings_{timestamp}.html"
    path.write_text(page, encoding="utf-8")
    shutil.copy(path, REPORTS_DIR / "latest.html")
    return path


if __name__ == "__main__":
    # Re-render latest.html from the last run's saved data - no new scan
    from .user_ratings_io import load_all
    saved = json.loads((REPORTS_DIR / "latest.json").read_text(encoding="utf-8"))
    ratings, notes, holdings, stale = load_all()
    from .ratings_runner import build_owned
    sections = [s for s in saved["sections"] if s[1] != "owned"]
    sections.append(["Owned", "owned", build_owned(holdings, ratings)])
    print(write_html_report(saved["header"], sections, ratings, notes,
                            holdings, stale, saved["timestamp"]))
