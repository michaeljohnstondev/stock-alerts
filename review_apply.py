"""Apply a review sheet: what you bought, skipped or rejected re-shapes the ladder.

A rung number is a commitment size, so what you actually did at a level is
the best evidence of what rung it really is. One mark per ticker:

    b 300 2  bought $300 worth, 2 shares. Dollar size sets the rung you
             reached (see rung_for); that rung and the ones above it are marked
             filled and new rungs below your price are built from chart structure.
    b 230 5 518
             an add: $230 for 5 shares, $518 the whole position after it. The
             rung is sized by the whole position - adding is "buy more".
    s        skipped / don't know. Moves up one rung: 2 becomes 1, 4 becomes 3,
             the old rung 1 drops off, and a new bottom rung is built below.
    radar    the old floor (deepest rung) becomes the new rung 1, with new
             rungs below it - "keep it on my radar, I'd start here".
    r        reject at any price. Every structure rung is muted.
    (blank)  not reviewed - untouched.

Rung 5 watch prices and sell ladders are always kept. Every change is filed
under "reviews" in the entry, old rungs included.

Usage:
    python review_apply.py reports/review_2026-11-02.txt         # dry run
    python review_apply.py reports/review_2026-11-02.txt --go    # write
"""

import json
import shutil
import sys
from datetime import date

from stock_checker.constants import ROOT_DIR, USER_RATINGS_FILE
from stock_checker.mutes import load_mutes, save_mutes, add_mute
from fill_levels import ladder_for, build_ladder, RUNGS
from suggest_levels import _nice

# Dollars in -> rung reached. Your sizing: under ~$200 is a starter (rung 1),
# $200-400 is "buy more" (rung 2), and ~$1,500-2,000 is as deep as you go on
# a stock you don't know well (KTOS, LEN, RH) - rung 4. One share counts as
# rung 1 whatever it costs - it is the smallest position the stock allows.
SIZE_BANDS = [(200, 1), (500, 2), (1500, 3)]   # under this many dollars -> rung


def rung_for(dollars: float, shares: float, total: float | None = None) -> int:
    """Rung from dollars, capped by share count.

    The cap is what keeps price honest: one $1,000 share is the smallest
    position that stock allows, not a rung 4. So 1 share is at most rung 1,
    2 shares at most rung 2, and so on. Cheap stocks never hit the cap.
    For an add, pass the whole position's share count, not just the new ones.
    """
    if total:
        dollars = total  # an add: the position size is the commitment
    by_dollars = next((r for cap, r in SIZE_BANDS if dollars < cap), 4)
    return max(1, min(by_dollars, int(shares)))


def parse_sheet(path) -> dict:
    marks = {}
    for line in open(path, encoding="utf-8"):
        body = line.split("#", 1)[0].split()
        if len(body) < 2:
            continue  # comment, blank, or ticker with no mark yet
        ticker, mark = body[0].upper(), body[1].lower()
        if mark == "b":
            # b <dollars> [shares] [position total, for an add]
            marks[ticker] = ("b", float(body[2]), float(body[3]) if len(body) > 3 else 2,
                             float(body[4]) if len(body) > 4 else None)
        elif mark in ("s", "radar", "r"):
            marks[ticker] = (mark,)
        else:
            raise SystemExit("  unknown mark %r on %s" % (mark, ticker))
    return marks


def rungs_below(ticker: str, anchor: float, count: int, start: int) -> list:
    """count new rungs from chart structure below anchor, rated from start."""
    if count <= 0:
        return []
    r = ladder_for(ticker)
    if r.get("error"):
        return []
    step = r["step"]
    zones = [z for z in r["all_zones"] if z["level"] < anchor * (1 - step)]
    picked = build_ladder(zones, anchor, step)[:count]
    out, seen = [], set()
    for z in picked:
        price = _nice(z["level"], r["cur"])
        if price in seen or price <= 0 or price >= anchor:
            continue
        seen.add(price)
        out.append({"price": price, "rating": start + len(out)})
    return out


def apply(ticker: str, entry: dict, mark: tuple, cur: float | None,
          rung: int | None = None) -> tuple:
    """rung overrides the size-based rung for a buy (your call wins)."""
    buys = entry.get("buys", [])
    watch = [b for b in buys if b["rating"] == 5]
    old = sorted([b for b in buys if b["rating"] != 5], key=lambda b: b["rating"])
    size = max(len(old), 2)
    today = date.today().isoformat()
    note = {"date": today, "mark": " ".join(str(m) for m in mark if m is not None), "buys": old}

    if mark[0] == "b":
        _, dollars, shares, total = mark
        price = dollars / shares
        # an add caps by the whole position's shares - about total / price
        n = rung or rung_for(dollars, round(total / price) if total else shares, total)
        # rungs above the one you reached: the deepest old rungs still
        # above your price, so the ladder reads as the path you took
        above = [b["price"] for b in old if b["price"] > price][-(n - 1):] if n > 1 else []
        filled = [{"price": p, "rating": i + 1, "filled": today} for i, p in enumerate(above)]
        filled.append({"price": round(price, 2), "rating": len(filled) + 1, "filled": today})
        new = filled + rungs_below(ticker, price, RUNGS - len(filled), len(filled) + 1)
        note["bought"] = {"dollars": dollars, "price": round(price, 2), "rung": n}
    elif mark[0] == "s":
        kept = [{"price": b["price"], "rating": i + 1} for i, b in enumerate(old[1:])]
        anchor = min([b["price"] for b in old] + ([cur] if cur else []))
        new = kept + rungs_below(ticker, anchor, size - len(kept), len(kept) + 1)
    elif mark[0] == "radar":
        floor = old[-1]["price"]
        anchor = min(floor, cur) if cur else floor
        new = [{"price": floor, "rating": 1}] + rungs_below(ticker, anchor, RUNGS - 1, 2)
    else:  # reject - ladder kept as is, muted below
        new = old

    entry.setdefault("reviews", []).append(note)
    entry["buys"] = sorted(new + watch, key=lambda b: b["rating"])
    if mark[0] != "r":
        entry["updated"] = today
    return entry, new


def note_of(mark: tuple) -> str:
    return " ".join("%g" % m if isinstance(m, float) else m for m in mark if m is not None)


def main():
    args = [a for a in sys.argv[1:] if a != "--go"]
    go = "--go" in sys.argv
    if not args:
        print(__doc__)
        return
    marks = parse_sheet(args[0])
    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    print("  %d marks%s\n" % (len(marks), "" if go else "  (DRY RUN - add --go to write)"))

    if go:
        stamp = date.today().isoformat()
        shutil.copy(USER_RATINGS_FILE, ROOT_DIR / "backups" / ("user_ratings.%s.pre_review.json" % stamp))

    rejects = []
    for ticker, mark in marks.items():
        entry = ratings.get(ticker)
        if not entry or not entry.get("buys"):
            print("  %-6s  --  no buy ladder" % ticker)
            continue
        try:
            r = ladder_for(ticker)
            cur = r.get("cur")
        except Exception:
            cur = None
        before = "  ".join("$%g(%d)" % (b["price"], b["rating"])
                           for b in sorted(entry["buys"], key=lambda b: b["rating"]) if b["rating"] != 5)
        entry, new = apply(ticker, json.loads(json.dumps(entry)) if not go else entry, mark, cur)
        after = "  ".join("$%g(%d)%s" % (b["price"], b["rating"], "f" if b.get("filled") else "")
                          for b in new)
        if mark[0] == "r":
            rejects.append(ticker)
        print("  %-6s %-18s %s\n  %-6s %-18s -> %s" % (ticker, note_of(mark), before, "", "", after))

    if go:
        USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
        if rejects:
            # A reject is a call on the business, not the price - a long mute
            mutes = load_mutes()
            for t in rejects:
                add_mute(mutes, t, None, "buy", days=180, note="rejected in review")
            save_mutes(mutes)
    print("\n  f = filled.  %s" % ("written" if go else "nothing written"))


if __name__ == "__main__":
    main()
