"""What the scanner said about a ticker at one moment - taken fresh when you
buy, so the record matches what you saw, not a report from hours earlier.

Saved with the buy in the ticker's review history; the card shows it next
to how the position has done since, so over time you can see which scores
were worth listening to.
"""

import json
from datetime import datetime

from .darkpool import score_all
from .data import get_live_price, get_ticker_info
from .market import fetch_earnings_calendar
from .notes import load_notes


def take(ticker: str, entry: dict) -> dict:
    # imported here: ratings_runner imports the page code, which reads this
    from .ratings_runner import _fetch_ticker_data, _zone
    td = _fetch_ticker_data(ticker, fetch_earnings_calendar())
    price = get_live_price(ticker) or td.get("price")
    high = td.get("high_52w")
    dp = (score_all([ticker])[0] or {}).get(ticker) or {}
    earn = td.get("earnings") or {}
    val = td.get("value") or {}
    rungs = sorted((b for b in entry.get("buys", []) if not b.get("filled")),
                   key=lambda b: b["rating"])
    hit = [b["rating"] for b in rungs if price and price <= b["price"]]
    row = _row_item(ticker, entry, rungs, price, high, td, dp, _zone)
    row = json.loads(json.dumps(row, default=str))  # dates -> text, so it saves
    return {
        "row": row,
        "at": datetime.now().isoformat(timespec="minutes"),
        "price": round(price, 2) if price else None,
        "zone": _zone(price, high),
        "off_high": round((1 - price / high) * 100, 1) if price and high else None,
        "ai": (load_notes().get(ticker) or {}).get("ai"),
        "dp": dp.get("score"), "dp_trend": dp.get("trend"),
        "dp_as_of": str(dp.get("as_of")) if dp.get("as_of") else None,
        "earn_days": earn.get("days_until"),
        "val": val.get("grade"), "val_score": val.get("score"),
        "sector": get_ticker_info(ticker).get("sector"),
        "rung_hit": max(hit) if hit else None,
    }


def describe(s: dict) -> str:
    """One line: "$173.25 | Zone 1 (20% off high) | AI A- | DP 4 (0) | ..."."""
    bits = [f'${s["price"]:.2f}' if s.get("price") else "?"]
    if s.get("zone") is not None:
        bits.append(f'Zone {s["zone"]} ({s.get("off_high", "?")}% off high)')
    if s.get("rung_hit"):
        bits.append(f'through rung {s["rung_hit"]}')
    for label, key in (("AI", "ai"), ("Val", "val")):
        if s.get(key):
            bits.append(f"{label} {s[key]}")
    if s.get("dp"):
        bits.append(f'DP {s["dp"]}' + (f' ({s["dp_trend"]})' if s.get("dp_trend") else ""))
    if s.get("earn_days") is not None:
        bits.append(f'EARN +{s["earn_days"]}d')
    if s.get("sector"):
        bits.append(s["sector"])
    return " | ".join(bits)


def _row_item(ticker, entry, rungs, price, high, td, dp, zone_fn) -> dict | None:
    """The buy-table row exactly as the report would print it right now:
    the deepest rung price is through, else the nearest one above it."""
    from .ratings_runner import _classify_entry
    from .ladder_roll import ladder_number
    if not price or not rungs:
        return None
    infos = [_classify_entry(price, b, "buy") for b in rungs]
    through = [i for i in infos if i["triggered"]]
    pick = max(through, key=lambda i: i["rating"]) if through else         min(infos, key=lambda i: i["pct_away"])
    return {
        "ticker": ticker, "price": price, "earnings": td.get("earnings"),
        "value": td.get("value"), "dp": dp or None,
        "zone": zone_fn(price, high), "rung_zone": zone_fn(pick["target"], high),
        "ladder": ladder_number(entry), "rungs": len(entry.get("buys", [])),
        "auto": entry.get("source") == "auto",
        "ai": (load_notes().get(ticker) or {}).get("ai"),
        **pick,
    }


def now_row(ticker: str, then: dict) -> dict | None:
    """The same row re-measured now, against the same rung as at the buy -
    so the two lines compare like for like."""
    entry = {"buys": [{"price": then["target"], "rating": then["rating"]}],
             "ladder": then.get("ladder", 1)}
    row = take(ticker, entry)["row"]
    if row:
        row["rungs"] = then.get("rungs", row["rungs"])
        row["auto"] = then.get("auto")
    return row
