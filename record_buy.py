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
    python record_buy.py --intend DOCN 1         # an open order: when its fill is
                                                 # recorded (sync or trade.py), use rung 1
"""

import json
import sys
from datetime import date

from stock_checker.constants import STATE_DIR, USER_RATINGS_FILE
from stock_checker.holdings import HOLDINGS_FILE, load_holdings
from stock_checker.snapshot import take, describe
from review_apply import apply, set_first_rung

# rung the user picked for an order that has not filled yet - used once, by the
# record() that books the fill, instead of sizing by dollars
INTENTS_FILE = STATE_DIR / "rung_intents.json"


def _intents() -> dict:
    return json.loads(INTENTS_FILE.read_text(encoding="utf-8")) if INTENTS_FILE.exists() else {}


def set_intent(ticker: str, rung: int):
    d = _intents()
    d[ticker.upper()] = int(rung)
    INTENTS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _pop_intent(ticker: str) -> int | None:
    d = _intents()
    rung = d.pop(ticker, None)
    if rung is not None:
        INTENTS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return rung


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
    if rung is None:
        rung = _pop_intent(ticker)
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


def record_sell(ticker: str, shares: float, price: float | None = None,
                note: str | None = None, update_holdings: bool = True) -> dict:
    """File a sale in the ticker's history: shares, fill, P/L vs your cost and
    the row as it stood when you sold. update_holdings=False when a sync is
    about to set holdings from the broker anyway."""
    ticker = ticker.upper()
    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    entry = ratings.setdefault(ticker, {"buys": []})
    snap = take(ticker, entry)
    if note:
        snap["note"] = note
    price = float(price) if price else snap["price"]
    holdings = load_holdings()
    positions = holdings.setdefault("positions", {})
    qty, basis = positions.get(ticker, (0, 0))
    pl = (price / basis - 1) * 100 if basis else None
    sold_all = shares >= qty
    entry.setdefault("reviews", []).append({
        "date": date.today().isoformat(), "mark": "sold",
        "sold": {"shares": shares, "price": round(price, 2), "basis": basis,
                 "pl_pct": round(pl, 1) if pl is not None else None,
                 "all": sold_all},
        "snapshot": snap})
    if sold_all and basis:
        # out of it: rung 1 goes back to your old entry, so a pullback to where
        # you bought shows up as a buy again (watch prices below it are kept).
        # Filled marks belonged to the position you just closed - clear them.
        entry["buys"] = [b for b in entry.get("buys", []) if not b.get("filled")]
        set_first_rung(ticker, entry, round(basis, 2))
        print("  %-6s re-entry  rung 1 at your old entry $%.2f" % (ticker, basis))
    USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
    print("  %-6s sold      %g @ $%.2f%s" % (ticker, shares, price,
                                             "  (%+.1f%% vs cost)" % pl if pl is not None else ""))
    if update_holdings and qty:
        left = qty - shares
        if left > 0:
            positions[ticker] = [left, basis]
        else:
            positions.pop(ticker, None)
        holdings["as_of"] = date.today().isoformat()
        HOLDINGS_FILE.write_text(json.dumps(holdings, indent=2), encoding="utf-8")
    return entry


def main():
    args = list(sys.argv[1:])
    intend = _opt(args, "--intend")
    if intend:
        set_intent(intend, int(args[0]))
        print(f"  {intend.upper()} - its next recorded fill will be rung {args[0]}")
        return
    price = _opt(args, "--price")
    rung = _opt(args, "--rung")
    if len(args) != 2:
        print(__doc__)
        return
    record(args[0], float(args[1]), price, int(rung) if rung else None)


if __name__ == "__main__":
    main()
