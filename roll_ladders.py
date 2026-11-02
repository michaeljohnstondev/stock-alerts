"""Roll dead buy ladders to a fresh one below the current price.

A ladder is dead when the last 3 closes are all more than one ATR under its
deepest rung (see stock_checker/ladder_roll.py). Rolling builds a new ladder
from chart structure, the same way fill_levels does, and files the old rungs
under "broken" - so the report's L number says how many floors gave way.

Only auto ($) ladders roll. Hand-set ones that are dead are listed as stale
for you to review by hand. Rung 5 watch prices and sell ladders are kept.

Usage:
    python roll_ladders.py                # dry run: what is dead, what it would roll to
    python roll_ladders.py --go           # roll every dead auto ladder
    python roll_ladders.py --go LCID FICO # roll just these (hand-set included -
                                          #   naming one is the review)
"""

import json
import shutil
import sys
from datetime import date

from stock_checker.constants import ROOT_DIR, USER_RATINGS_FILE
from stock_checker.data import fetch_stock_data
from stock_checker.ladder_roll import atr_pct, floor_rung, is_dead, ladder_number
from fill_levels import ladder_for


def _dead_check(ticker: str, entry: dict) -> dict | None:
    df = fetch_stock_data(ticker, period="3mo")
    if df is None or len(df) < 20:
        return None
    closes = [float(c) for c in df["Close"].tail(5)]
    atr = atr_pct(df)
    floor = floor_rung(entry.get("buys", []))
    return {"dead": is_dead(entry.get("buys", []), closes, atr),
            "cur": closes[-1], "floor": floor, "atr": atr}


def roll(entry: dict, new_buys: list, cur: float) -> dict:
    old = [b for b in entry.get("buys", []) if b["rating"] != 5]
    watch = [b for b in entry.get("buys", []) if b["rating"] == 5]
    entry.setdefault("broken", []).append({
        "ladder": ladder_number(entry),
        "buys": old,
        "set": entry.get("updated"),
        "rolled": date.today().isoformat(),
        "price": round(cur, 2),
    })
    entry["ladder"] = ladder_number(entry) + 1
    entry["buys"] = sorted(new_buys + watch, key=lambda b: b["rating"])
    entry["updated"] = date.today().isoformat()
    entry["source"] = "auto"
    return entry


def main():
    args = list(sys.argv[1:])
    go = "--go" in args
    if go:
        args.remove("--go")
    named = [a.upper() for a in args]

    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    todo = named or [t for t, e in ratings.items() if floor_rung(e.get("buys", []))]
    print("  checking %d ladders%s\n" % (len(todo), "" if go else "  (DRY RUN - add --go to write)"))

    rolls, stale = [], []
    for ticker in todo:
        entry = ratings.get(ticker)
        if not entry or not floor_rung(entry.get("buys", [])):
            print("  %-6s  --  no buy ladder" % ticker)
            continue
        chk = _dead_check(ticker, entry)
        if chk is None:
            continue
        if not named and not chk["dead"]:
            continue
        if entry.get("source") != "auto" and not named:
            stale.append((ticker, chk))
            continue
        rolls.append((ticker, chk))

    if go and rolls:
        backup = ROOT_DIR / "backups" / ("user_ratings.%s.pre_roll.json" % date.today().isoformat())
        backup.parent.mkdir(exist_ok=True)
        shutil.copy(USER_RATINGS_FILE, backup)

    rolled = 0
    for ticker, chk in rolls:
        entry = ratings[ticker]
        below = (1 - chk["cur"] / chk["floor"]) * 100
        try:
            r = ladder_for(ticker)
        except Exception as e:
            r = {"error": str(e)[:50]}
        # No structure under a fresh low is an answer too: the ladder goes
        # empty and the zone alerts keep watching it on the way down.
        new_buys = [] if r.get("error") else r["buys"]
        lad = "  ".join("$%.2f(%d)" % (b["price"], b["rating"]) for b in new_buys) \
            or "no structure below - zones only"
        print("  %-6s $%-8.2f %4.1f%% under floor $%.2f   L%d -> L%d   %s"
              % (ticker, chk["cur"], below, chk["floor"],
                 ladder_number(entry), ladder_number(entry) + 1, lad))
        if go:
            roll(entry, new_buys, chk["cur"])
            rolled += 1

    if stale:
        print("\n  STALE - hand-set and through the floor, not rolled (review, then"
              " roll by name):")
        for ticker, chk in stale:
            print("  %-6s $%-8.2f %4.1f%% under floor $%.2f"
                  % (ticker, chk["cur"], (1 - chk["cur"] / chk["floor"]) * 100, chk["floor"]))

    if go and rolled:
        USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
    print("\n  %s %d, stale %d%s" % ("rolled" if go else "would roll", len(rolls), len(stale),
                                     "" if go else "  (nothing written)"))


if __name__ == "__main__":
    main()
