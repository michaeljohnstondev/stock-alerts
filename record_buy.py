"""Record a buy in one step - snapshot, ladder, holdings, sell ladder.

At the moment you buy it rescans the one ticker, so the record holds what
the scanner said right then (price, zone, AI, DP, Val, earnings, rung), not
what a report showed hours ago. Then it marks the rung filled and builds new
rungs below (review_apply's rules: dollars set the rung, capped by shares,
adds sized by the whole position), updates state/holdings.json, and builds a
sell ladder for a new position.

Usage:
    python record_buy.py SCCO 2                  # 2 shares at the live price
    python record_buy.py SCCO 2 --price 173.25   # your actual fill
    python record_buy.py SCCO 2 --rung 1         # override the rung sizing
"""

import json
import sys
from datetime import date

from stock_checker.constants import USER_RATINGS_FILE
from stock_checker.holdings import HOLDINGS_FILE, load_holdings
from stock_checker.snapshot import take, describe
from review_apply import apply


def _opt(args: list, flag: str):
    if flag in args:
        i = args.index(flag)
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def record(ticker: str, shares: float, price: float | None = None,
           rung: int | None = None, note: str | None = None) -> dict:
    """Snapshot, fill the rung, update holdings, sell ladder for a new name.
    note is saved on the snapshot (e.g. "taken at sync, not at the buy")."""
    ticker = ticker.upper()
    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    entry = ratings.setdefault(ticker, {"buys": []})
    snap = take(ticker, entry)
    if note:
        snap["note"] = note
    price = float(price) if price else snap["price"]
    print("  %-6s snapshot  %s" % (ticker, describe(snap)))

    holdings = load_holdings()
    positions = holdings.setdefault("positions", {})
    old_qty, old_basis = positions.get(ticker, (0, 0))
    dollars = shares * price
    total = old_qty * old_basis + dollars if old_qty else None

    entry, new = apply(ticker, entry, ("b", dollars, shares, total), snap["price"], rung=rung)
    rec = entry["reviews"][-1]
    rec["bought"]["shares"] = shares
    rec["snapshot"] = snap
    print("  %-6s ladder    %s" % (ticker, "  ".join(
        "$%g(%d)%s" % (b["price"], b["rating"], "f" if b.get("filled") else "") for b in new)))

    qty = old_qty + shares
    positions[ticker] = [qty, round((old_qty * old_basis + dollars) / qty, 2)]
    holdings["as_of"] = date.today().isoformat()
    HOLDINGS_FILE.write_text(json.dumps(holdings, indent=2), encoding="utf-8")
    print("  %-6s holding   %g @ $%.2f  (%d positions)" % (ticker, qty, positions[ticker][1], len(positions)))

    if not entry.get("sells"):
        from fill_sells import ladder_for as sell_ladder
        r = sell_ladder(ticker)
        if not r.get("error"):
            entry["sells"], entry["sells_source"] = r["sells"], "auto"
            entry["sells_updated"] = date.today().isoformat()
            print("  %-6s sells     %s" % (ticker, "  ".join(
                "$%g(%d)" % (s["price"], s["rating"]) for s in r["sells"])))

    USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
    return entry


def main():
    args = list(sys.argv[1:])
    price = _opt(args, "--price")
    rung = _opt(args, "--rung")
    if len(args) != 2:
        print(__doc__)
        return
    record(args[0], float(args[1]), price, int(rung) if rung else None)


if __name__ == "__main__":
    main()
