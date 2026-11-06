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
_BARS = {}    # ticker -> a year of daily candles, for the charts
MINI_DAYS = 126  # the row chart: about six months of trading days


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


# More than this share of your money in one sector puts a warning on the page
OVEREXPOSED_PCT = 30
# sector ETFs count toward the sector they hold; broad index ones count toward none
_ETF_SECTOR = {
    "SOXX": "Technology", "SMH": "Technology", "XLK": "Technology", "QQQ": "Technology",
    "IGV": "Technology", "ARKK": "Technology", "XLI": "Industrials",
    "XLF": "Financial Services", "KRE": "Financial Services", "XLE": "Energy",
    "XLU": "Utilities", "XLV": "Healthcare", "XBI": "Healthcare",
    "XLP": "Consumer Defensive", "XLY": "Consumer Cyclical", "XLC": "Communication Services",
    "XLB": "Basic Materials", "XLRE": "Real Estate", "URA": "Energy", "KWEB": "Communication Services",
}


_SECT = {}   # ticker -> sector, for the overexposed tint
_HEAVY = set()  # sectors over OVEREXPOSED_PCT this run


def _sector_of(tk: str):
    s = _ETF_SECTOR.get(tk) or (_SECT.get(tk) or {}).get("sector")
    return s if s and s != "ETF" else None


def _ox(tk: str) -> str:
    """Pink-tint class for a ticker in a sector you're overexposed to."""
    return " ox" if _sector_of(tk) in _HEAVY else ""


def _exposure_banner(owned: list) -> str:
    """A loud strip at the top of the page for any sector holding more than
    OVEREXPOSED_PCT of the money in your positions. Also sets _HEAVY, so every
    row and card in those sectors gets tinted."""
    from .constants import SECTOR_CACHE_FILE
    try:
        _SECT.update(json.loads(SECTOR_CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    _HEAVY.clear()
    total = sum(it["value_usd"] for it in owned)
    if not total:
        return ""
    by = {}
    for it in owned:
        tk = it["ticker"]
        s = _sector_of(tk)
        if s:
            v, names = by.get(s, (0, []))
            by[s] = (v + it["value_usd"], names + [(it["value_usd"], tk)])
    lines, key = [], []
    for s, (v, names) in sorted(by.items(), key=lambda x: -x[1][0]):
        pct = v / total * 100
        if pct <= OVEREXPOSED_PCT:
            break
        _HEAVY.add(s)
        key.append(f"{s}:{int(pct // 5 * 5)}")
        top = ", ".join(tk for _, tk in sorted(names, reverse=True)[:6])
        lines.append(f'<div><b>{pct:.0f}% in {_e(s)}</b> &mdash; ${v:,.0f} of ${total:,.0f} '
                     f'across {len(names)} positions (biggest: {_e(top)})</div>')
    if not lines:
        return ""
    # closing it lasts until the picture changes (another sector, or 5 points more)
    return (f'<div class="overexp" data-key="{_e("|".join(key))}">'
            '<button class="oe-x" title="Hide until this changes">&times;</button>'
            '<div class="oe-t">&#9888; You&rsquo;re overexposed</div>'
            + "".join(lines)
            + f'<div class="oe-n">Over {OVEREXPOSED_PCT}% of your money in one sector moves as one bet. '
              'Pink rows and cards are in that sector.</div></div>')


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
    zone, ai, o1, o5, o20, dp, earn, val, details = common
    return [owned, tick, price, _mini(ticker)] + mid + [zone, ai, o1, o5, o20, rung, dp, earn, val,
                                         _sector_cell(ticker), details]

def _mini(ticker: str) -> str:
    """Six months of daily candles, drawn by the page's script when scrolled
    into view. Sorts by the change over those six months."""
    bars = _BARS.get(ticker)
    if not bars:
        return _td("", "")
    b = bars[-MINI_DAYS:]
    chg = (b[-1][4] / b[0][1] - 1) * 100
    return (f'<td data-v="{chg:.1f}" title="{chg:+.1f}% over {len(b)} days">'
            f'<a href="cards/{_e(ticker)}.html" target="_blank">'
            f'<canvas class="mini" data-t="{_e(ticker)}"></canvas></a></td>')


# Set per run from config.json "chart_site" (see charts.py)
_chart = lambda t: f"https://www.tradingview.com/chart/?symbol={t}"

CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--mute:#6b6b70;--line:#e4e2dd;--card:#fff;
--acc:#2457c5;--up:#1b7f4b;--down:#b3261e;--warn:#9a6700;--chip:#f0eee9;--ox:#fde0de}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--mute:#9a9aa2;
--line:#2c2c31;--card:#1c1c20;--acc:#7aa2ff;--up:#5cc58c;--down:#ff8a80;--warn:#e3b341;--chip:#26262b;--ox:#4a1f1d}}
tr.ox td{background:var(--ox)} .card.ox{background:var(--ox);border-left:4px solid #d93025}
/* phones: scroll the table sideways instead of crushing Details into a tall
   column, and keep the ticker pinned on the left while you scroll */
@media (max-width:700px){
main{padding:12px 10px 60px} td,th{padding:6px 8px;font-size:13px}
td.tags{min-width:300px;font-size:12px;line-height:1.35}
td:nth-child(2),th:nth-child(2){position:sticky;left:0;z-index:1;background:var(--bg);
box-shadow:1px 0 0 var(--line)}
tr.ox td:nth-child(2){background:var(--ox)}
.card td:nth-child(2),.card th:nth-child(2){background:var(--card)}}
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
.tabs .tog{margin-left:auto} .tabs .tog+.tog{margin-left:0} .tabs .tog.on{background:var(--warn);border-color:var(--warn);color:#fff}
.tabs .ox-t{border-color:#d93025} .tabs .ox-t.on{background:#d93025;border-color:#d93025}
.overexp{position:relative;border:2px solid #d93025;border-radius:12px;padding:12px 40px 12px 16px;
margin:12px 0 4px;font-size:14px}
.overexp .oe-t{color:#d93025;font-size:16px;font-weight:700;margin-bottom:4px}
.overexp .oe-n{color:var(--mute);font-size:12.5px;margin-top:4px}
.overexp .oe-x{position:absolute;top:6px;right:8px;font:inherit;font-size:20px;line-height:1;
background:none;border:0;color:var(--mute);cursor:pointer;padding:4px 8px}
canvas.mini{display:block;width:170px;height:34px}
.rng{display:flex;gap:6px;margin:6px 0} .rng button{font:inherit;font-size:12px;background:var(--chip);
color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:3px 10px;cursor:pointer}
.rng button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
canvas.big{display:block;width:100%;height:300px} #ohlc{min-height:1.4em}
.find{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0 4px}
.find input{font:inherit;font-size:16px;width:100%;box-sizing:border-box;padding:8px 12px;border-radius:10px;
border:1px solid var(--line);background:var(--card);color:var(--fg)}
"""

JS = """
function openHash(){var id=location.hash.slice(1);if(!id)return;var d=document.getElementById(id);
if(d&&d.tagName==='DETAILS'){d.open=true;d.scrollIntoView({block:'start'});}}
window.addEventListener('hashchange',openHash);window.addEventListener('load',openHash);
// "New only" hides lines that are the same as last scan (the short report's filter)
var NEW=false;try{NEW=localStorage.getItem('newOnly')==='1'}catch(e){}
function match(f,k,r){return (f==='all'&&k!=='owned')||f===k||(f.charAt(0)==='r'&&k==='buy'&&r.dataset.r===f.slice(1));}
// "Hide pink" drops lines in your overexposed sectors; your own positions stay
var OX=false;try{OX=localStorage.getItem('hideOx')==='1'}catch(e){}
function keep(k,r){return (!NEW||k==='owned'||r.dataset.n==='1')&&!(OX&&k!=='owned'&&r.classList.contains('ox'));}
// a search overrides the tabs and New only: every line for matching tickers, owned included
var Q='';function hit(r){return r.dataset.t.indexOf(Q)===0;}
function shown(f,k,r){return Q?hit(r):match(f,k,r)&&keep(k,r);}
function eachRow(fn){[].forEach.call(document.querySelectorAll('section'),function(s){var k=s.dataset.kind;
[].forEach.call(s.querySelectorAll('tr[data-k]'),function(r){fn(s,k,r)});});}
function render(){var on=document.querySelector('.tabs button.on[data-f]'),f=on?on.dataset.f:'all';
[].forEach.call(document.querySelectorAll('.tabs button[data-f]'),function(b){var c=0,g=b.dataset.f;
eachRow(function(s,k,r){if(match(g,k,r)&&keep(k,r))c++;});b.querySelector('.n').textContent=c;});
eachRow(function(s,k,r){r.style.display=shown(f,k,r)?'':'none';});
[].forEach.call(document.querySelectorAll('section'),function(s){
var n=0;[].forEach.call(s.querySelectorAll('tr[data-k]'),function(r){if(r.style.display!=='none')n++;});
s.style.display=n?'':'none';});
document.getElementById('newonly').classList.toggle('on',NEW);
var hx=document.getElementById('hideox');if(hx)hx.classList.toggle('on',OX);}
function showTab(btn){[].forEach.call(document.querySelectorAll('.tabs button[data-f]'),function(b){b.classList.toggle('on',b===btn)});render();}
document.addEventListener('click',function(e){var b=e.target.closest('.tabs button[data-f]');if(b)showTab(b);
if(e.target.closest('#newonly')){NEW=!NEW;try{localStorage.setItem('newOnly',NEW?'1':'0')}catch(x){}render();}
if(e.target.closest('#hideox')){OX=!OX;try{localStorage.setItem('hideOx',OX?'1':'0')}catch(x){}render();}});
(function(){var b=document.querySelector('.overexp');if(!b)return;var k=b.dataset.key;
try{if(localStorage.getItem('oeHidden')===k)b.style.display='none'}catch(e){}
b.querySelector('.oe-x').addEventListener('click',function(){b.style.display='none';
try{localStorage.setItem('oeHidden',k)}catch(e){}});})();
document.addEventListener('input',function(e){if(e.target.id==='q'){Q=e.target.value.trim().toUpperCase();render();}});
window.addEventListener('load',render);
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

# Candles on canvas, from C (ticker -> [[date, o, h, l, c]]). Row charts draw
# only once scrolled into view; a card's big chart has range buttons, your
# open buy rungs (dashed) and cost (solid) as lines, and a tap readout.
CHART_JS = """
function cv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
function drawC(c,bars,o){o=o||{};var W=c.clientWidth,H=c.clientHeight,d=window.devicePixelRatio||1;
c.width=W*d;c.height=H*d;var g=c.getContext('2d');g.scale(d,d);g.clearRect(0,0,W,H);
if(!bars||!bars.length)return null;var R=o.right||0,T=o.top||2,B=o.bottom||2;
var lo=Infinity,hi=-Infinity;bars.forEach(function(b){lo=Math.min(lo,b[3]);hi=Math.max(hi,b[2]);});
var sp=hi-lo,ls=(o.lines||[]).filter(function(l){return l.p>=lo-sp*.2&&l.p<=hi+sp*.2;});
ls.forEach(function(l){lo=Math.min(lo,l.p);hi=Math.max(hi,l.p);});
var n=bars.length,cw=(W-R)/n,y=function(v){return T+(hi-v)/((hi-lo)||1)*(H-T-B);};
var up=cv('--up'),dn=cv('--down'),mu=cv('--mute');g.font='11px system-ui,sans-serif';
if(o.axis){g.fillStyle=mu;var m='';bars.forEach(function(b,i){var k=b[0].slice(0,7);
if(k!==m){if(m){g.fillRect(i*cw,H-B,1,4);g.fillText(new Date(b[0]+'T12:00').toLocaleString('en',{month:'short'}),i*cw+3,H-2);}m=k;}});
[hi,lo].forEach(function(v){g.fillText('$'+v.toFixed(2),W-R+4,y(v)+4);});}
bars.forEach(function(b,i){var x=Math.round(i*cw+cw/2)+.5,col=b[4]>=b[1]?up:dn;g.strokeStyle=g.fillStyle=col;
g.lineWidth=1;g.beginPath();g.moveTo(x,y(b[2]));g.lineTo(x,y(b[3]));g.stroke();
var w=Math.max(cw*.7,1),t=y(Math.max(b[1],b[4])),h=Math.max(Math.abs(y(b[1])-y(b[4])),1);
g.fillRect(x-w/2,t,w,h);});
ls.forEach(function(l){var yy=Math.round(y(l.p))+.5;g.strokeStyle=g.fillStyle=cv(l.c);g.setLineDash(l.dash?[4,3]:[]);
g.beginPath();g.moveTo(0,yy);g.lineTo(W-R,yy);g.stroke();g.setLineDash([]);if(R)g.fillText(l.t,W-R+4,yy+4);});
return {cw:cw};}
(function(){var io='IntersectionObserver' in window?new IntersectionObserver(function(es){es.forEach(function(e){
if(e.isIntersecting){io.unobserve(e.target);drawC(e.target,(C[e.target.dataset.t]||[]).slice(-MINI));}});},{rootMargin:'300px'}):null;
[].forEach.call(document.querySelectorAll('canvas.mini'),function(c){if(io)io.observe(c);
else drawC(c,(C[c.dataset.t]||[]).slice(-MINI));});
var big=document.getElementById('big');if(!big)return;var all=C[big.dataset.t]||[],n=MINI,info,shown;
function go(){shown=all.slice(-n);info=drawC(big,shown,{right:70,top:8,bottom:16,axis:1,lines:L});
[].forEach.call(document.querySelectorAll('.rng button'),function(b){b.classList.toggle('on',+b.dataset.n===n);});}
document.querySelector('.rng').addEventListener('click',function(e){var b=e.target.closest('button');if(b){n=+b.dataset.n;go();}});
function show(e){if(!info)return;var r=big.getBoundingClientRect(),i=Math.floor((e.clientX-r.left)/info.cw);
var b=shown[Math.max(0,Math.min(shown.length-1,i))];
document.getElementById('ohlc').textContent=b[0]+'  O '+b[1]+'  H '+b[2]+'  L '+b[3]+'  C '+b[4]+
'  ('+((b[4]/b[1]-1)*100).toFixed(1)+'%)';}
big.addEventListener('pointermove',show);big.addEventListener('pointerdown',show);
window.addEventListener('resize',go);go();})();
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
            _grade_td(item.get("o1")), _grade_td(item.get("o5")), _grade_td(item.get("o20")),
            _td(_e(dp_txt), sc or "", dp_cls),
            _td(_e(earn_txt.strip()), du if du is not None else 999, earn_cls),
            _td(_e(val_txt), val.get("score", "") if grade else "",
                "g-a" if grade[:1] in ("A", "B") else "g-d" if grade[:1] in ("D", "F") else ""),
            f'<td class="tags">{_e(detail)}</td>']


def _grade_td(g) -> str:
    """An A-F grade cell, colored like AI: A-B green, C plain, D-F red."""
    g = g or ""
    cls = "g-a" if g[:1] in ("A", "B") else "g-d" if g and g[:1] in "DF" else ""
    return _td(_e(g), _grade_v(g), cls)


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
        _td(f'{item["off_high"]:.1f}%', item["off_high"], "num"),  # .1f: zone lines sit on 10/25/40%
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
    cols = ["Owned", "Ticker", "Price", "6 mo"] + mid + ["Zone", "AI", "O-1", "O-5", "O-20", "Rung", "DP", "Earn", "Val",
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
        line = f'bought ${b["dollars"]:,.0f} at ${b["price"]:.2f} - ' +             ("above your ladder" if b.get("above_ladder") else f'rung {b["rung"]}')
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
        for key, label in (("o1", "O-1 - a year out"), ("o5", "O-5 - five years out"), ("o20", "O-20 - twenty years out")):
            if note.get(key):
                bits.append(f'<p><b>{label}: {_e(note[key])}</b> - {_e(note.get(key + "_why", ""))}</p>')
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


def _big_chart(t: str) -> str:
    if t not in _BARS:
        return ""
    btn = "".join(f'<button data-n="{n}">{lbl}</button>' for n, lbl in ((63, "3M"), (MINI_DAYS, "6M"), (252, "1Y")))
    return (f'<div class="lbl">Daily chart</div><div class="rng">{btn}</div>'
            f'<canvas id="big" class="big" data-t="{_e(t)}"></canvas><div id="ohlc" class="meta"></div>')


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
            n = notes.get(it["ticker"]) or {}
            it["ai"], it["o1"], it["o5"], it["o20"] = n.get("ai"), n.get("o1"), n.get("o5"), n.get("o20")

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

    from .candles import load_candles
    _BARS.clear()
    _BARS.update(load_candles(sorted({it["ticker"] for _, _, items in sections for it in items})))

    banner = _exposure_banner(next((items for _, k, items in sections if k == "owned"), []))
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
            # zone moves are news by definition; ladder lines only when flagged
            n = ' data-n="1"' if zone or it.get("escalated") else ""
            ox = ' class="ox"' if _ox(it["ticker"]) else ""
            rows.append(row.replace("<tr", f'<tr data-k="{kind}" data-t="{it["ticker"]}"{n}{ox}', 1))
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
    tab_html = '<div class="find"><input id="q" type="search" placeholder="Find ticker" ' \
        'autocomplete="off" autocapitalize="characters" spellcheck="false"></div><div class="tabs">' + "".join(
        f'<button data-f="{f}"{" class=on" if f == "all" else ""}>{label}'
        f'<span class="n">{total if f == "all" else counts.get(f, 0)}</span></button>'
        for f, label in tabs) + '<button id="newonly" class="tog">New only</button>' \
        + ('<button id="hideox" class="tog ox-t">Hide pink</button>' if _HEAVY else "") \
        + "</div>"  # always shown, 0 when empty

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
        card = card.replace('<div class="card">', f'<div class="card{_ox(t)}">{_big_chart(t)}{rows_html}', 1)
        # lines on the big chart: open buy rungs, and your cost if you hold it
        lines = [{"p": b["price"], "t": f'{"*" * b["rating"]} ${b["price"]:g}', "c": "--warn", "dash": 1}
                 for b in ratings.get(t, {}).get("buys", []) if not b.get("filled")]
        if positions.get(t):
            lines.append({"p": positions[t][1], "t": f'cost ${positions[t][1]:.2f}', "c": "--acc"})
        data = json.dumps({t: _BARS.get(t, [])}) if t in _BARS else "{}"
        (card_dir / f"{t}.html").write_text(
            f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_e(t)}</title><style>{CSS}</style></head><body><main>'
            f'{card}<p class="meta">as of run {_e(timestamp)}</p></main>'
            f'<script>var C={data},L={json.dumps(lines)},MINI={MINI_DAYS};{CHART_JS}</script></body></html>',
            encoding="utf-8")
    mini = {t: _BARS[t][-MINI_DAYS:] for t in seen if t in _BARS}
    page = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Ratings {timestamp}</title><style>{CSS}</style></head><body><main id="top">'
            + banner + f'<h1>Stock Ratings</h1><div class="meta">'
            + "".join(f"<div>{_e(h)}</div>" for h in header)
            + '<div>&#9733; = rung (&#9733;&#9733;&#9733;&#9733; = rung 4, &#9733;&#9733;&#9733;&#9733;&#9733; = watch price). Click a ticker for its card,'
              ' a column header to sort (again to reverse).</div>'
            + "</div>" + tab_html + "".join(body)
            + f"</main><script>{JS}</script><script>var C={json.dumps(mini)},L=[],MINI={MINI_DAYS};"
            + CHART_JS + "</script></body></html>")
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
