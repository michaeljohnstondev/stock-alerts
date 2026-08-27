"""Bulk-fill buy ladders for unrated tickers using suggest_levels structure.

Picks four rungs from the candidate zones, spaced far enough apart that each
one is a genuinely different price for THAT stock (spacing scales to its own
volatility), and preferring well-supported zones for the deep rungs.

Never touches a ticker that already has ratings. Everything it writes is
tagged "source": "auto" so hand-set levels stay distinguishable.

Usage:
    python fill_levels.py --dry AAPL MSFT     # show what it would write
    python fill_levels.py --dry               # dry run over every unrated
    python fill_levels.py                     # write them
    python fill_levels.py --limit 50          # stop after 50 tickers
    python fill_levels.py --force MU MSFT     # overwrite existing ladders
"""

import json
import sys
from datetime import date

import numpy as np

from stock_checker.constants import USER_RATINGS_FILE
from stock_checker.data import fetch_stock_data, get_live_price
from suggest_levels import (
    STRUCTURE_DAYS, find_shelves, find_pivot_lows, find_unfilled_gaps,
    project_mas, build_candidates, _nice,
)

RUNGS = 4
MIN_STEP_FLOOR = 0.020   # rungs never closer than 2%
MIN_STEP_CAP = 0.070     # ...nor forced further than 7% apart
WEAK_ZONE_FRAC = 0.15    # drop zones scoring under this share of the best


def _atr_pct(df, period: int = 14) -> float:
    """Average true range as a fraction of price - the stock's own scale."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev).abs(), (low - prev).abs()))
    atr = tr.tail(period).mean()
    return float(atr / close.iloc[-1])


def build_ladder(zones: list, cur: float, min_step: float) -> list:
    """Choose RUNGS levels, descending, each a real move below the last.

    Walks candidates from the top down, taking one whenever it clears the
    previous rung by min_step. Within the window that clears it, the
    highest-scoring zone wins - so a rung lands on the best structure
    available at that depth rather than merely the first thing encountered.
    """
    if not zones:
        return []
    # Score on raw structural weight, NOT the display score. The display score
    # fades levels far below spot so the ranked list stays actionable - but a
    # rung 4 is meant to be far below spot, and fading it is how the deep
    # bases (NEE $70, CL $75) get dropped before they can be picked.
    for z in zones:
        z["raw"] = sum(m["weight"] for m in z["members"])
    best = max(z["raw"] for z in zones)
    live = [z for z in zones if z["raw"] >= best * WEAK_ZONE_FRAC]
    live.sort(key=lambda z: -z["level"])

    picked = []
    for z in live:
        if picked and z["level"] > picked[-1]["level"] * (1 - min_step):
            continue  # too close to the rung above it
        # Among zones bunched just below this one, prefer the strongest
        window = [w for w in live
                  if abs(w["level"] - z["level"]) / cur <= min_step * 0.5
                  and (not picked or w["level"] <= picked[-1]["level"] * (1 - min_step))]
        pick = max(window, key=lambda w: w["raw"]) if window else z
        if picked and pick["level"] > picked[-1]["level"] * (1 - min_step):
            continue
        picked.append(pick)
        if len(picked) == RUNGS:
            break
    return picked


def ladder_for(ticker: str) -> dict:
    df = fetch_stock_data(ticker, period="3y")
    if df is None or df.empty or len(df) < 220:
        return {"ticker": ticker, "error": "thin history"}
    cur = get_live_price(ticker) or float(df["Close"].iloc[-1])
    recent = df.tail(STRUCTURE_DAYS)
    zones = build_candidates(cur, find_shelves(recent, cur), find_pivot_lows(recent),
                             find_unfilled_gaps(recent), project_mas(df))
    if not zones:
        return {"ticker": ticker, "error": "no candidates"}

    step = min(MIN_STEP_CAP, max(MIN_STEP_FLOOR, _atr_pct(df) * 1.5))
    picked = build_ladder(zones, cur, step)
    # Too strict a step can starve the ladder - ease off until four fit
    while len(picked) < RUNGS and step > MIN_STEP_FLOOR:
        step = max(MIN_STEP_FLOOR, step * 0.75)
        picked = build_ladder(zones, cur, step)
    if len(picked) < 2:
        return {"ticker": ticker, "error": "too few levels"}

    buys, seen = [], set()
    for i, z in enumerate(picked, start=1):
        price = _nice(z["level"], cur)
        if price in seen or price <= 0:
            continue
        seen.add(price)
        buys.append({"price": price, "rating": len(buys) + 1})
    return {"ticker": ticker, "cur": cur, "step": step, "buys": buys,
            "kinds": [sorted(z["kinds"]) for z in picked]}


def main():
    args = list(sys.argv[1:])
    dry = "--dry" in args
    if dry:
        args.remove("--dry")
    # Overwriting an existing ladder must be deliberate: a ladder price has
    # already fallen through is carrying information, and regenerating it
    # around the new spot would erase exactly that.
    force = "--force" in args
    if force:
        args.remove("--force")
    limit = None
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
        del args[i:i + 2]

    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    if args:
        todo = [t.upper() for t in args]
    else:
        cfg = json.loads((USER_RATINGS_FILE.parent / "config.json").read_text(encoding="utf-8"))
        todo = [t for t in cfg["tickers"] if t not in ratings]
    if limit:
        todo = todo[:limit]

    print("  %d tickers to fill%s\n" % (len(todo), "  (DRY RUN)" if dry else ""))
    done = failed = 0
    for n, ticker in enumerate(todo, start=1):
        if ticker in ratings and not dry and not force:
            continue  # never overwrite a hand-set ladder
        try:
            r = ladder_for(ticker)
        except Exception as e:
            r = {"ticker": ticker, "error": str(e)[:50]}
        if r.get("error"):
            failed += 1
            print("  [%d/%d] %-6s  --  %s" % (n, len(todo), ticker, r["error"]))
            continue

        lad = "  ".join("$%.2f(%d)" % (b["price"], b["rating"]) for b in r["buys"])
        print("  [%d/%d] %-6s $%-9.2f step %.1f%%   %s"
              % (n, len(todo), ticker, r["cur"], r["step"] * 100, lad))
        if not dry:
            prev = ratings.get(ticker, {})
            entry = {"buys": r["buys"],
                     "updated": date.today().isoformat(),
                     "source": "auto"}
            # keep any 5-star watch and any sell ladder already set
            watch = [b for b in prev.get("buys", []) if b["rating"] == 5]
            if watch:
                entry["buys"] = sorted(entry["buys"] + watch, key=lambda e: e["rating"])
            for k in ("sells", "sells_source"):
                if k in prev:
                    entry[k] = prev[k]
            ratings[ticker] = entry
            USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
        done += 1

    print("\n  filled %d, failed %d%s" % (done, failed, "  (nothing written)" if dry else ""))


if __name__ == "__main__":
    main()
