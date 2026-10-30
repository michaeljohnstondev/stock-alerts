"""Shadow ladders - fresh buy ladders kept beside the live ones, for comparison.

The live ladders in .user_ratings.json were mostly built in August off
August's price structure. This builds a second set from today's structure
with the same fill_levels logic, WITHOUT touching the live file, then scores
both sets against what price actually does from here on.

    python shadow_ladders.py generate     # build fresh set + snapshot live set
    python shadow_ladders.py compare      # score both against price since then

generate also freezes a copy of the live ladders, so editing .user_ratings.json
afterwards does not move the goalposts - both sets start the race together.

How a rung is scored (buy side):

    touched   a daily low reached the rung
    held      after the touch, price rallied one step above the rung
              before it fell one step below
    broke     it fell one step below first
    pending   touched, neither yet

"One step" is the stock's own ladder spacing (1.5x ATR, clamped 2-7%),
the same for both sets on a ticker so neither is graded on an easier curve.
If a single day hits both, it counts as broke - daily bars cannot say which
came first, so the tie goes against the level.

Rungs already at or above price when the race starts are left out: they
were through before the test began and cannot be scored on it.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from stock_checker.constants import ROOT_DIR, USER_RATINGS_FILE
from stock_checker.data import fetch_stock_data
from fill_levels import ladder_for, _atr_pct, MIN_STEP_FLOOR, MIN_STEP_CAP

SHADOW_FILE = ROOT_DIR / ".shadow_ladders.json"


def _step(ticker: str) -> float:
    df = fetch_stock_data(ticker, period="3y")  # cached by ladder_for's fetch
    return min(MIN_STEP_CAP, max(MIN_STEP_FLOOR, _atr_pct(df) * 1.5))


def generate() -> None:
    if SHADOW_FILE.exists() and "--force" not in sys.argv:
        prev = json.loads(SHADOW_FILE.read_text(encoding="utf-8"))
        print(f"  A shadow set from {prev['generated']} already exists - "
              f"regenerating restarts the race.\n  Use --force to replace it.")
        return

    live = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    tickers = sorted(live)
    print(f"  Building fresh ladders for {len(tickers)} tickers...")

    out, failed = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(ladder_for, t): t for t in tickers}
        for n, fut in enumerate(as_completed(futures), start=1):
            t = futures[fut]
            try:
                r = fut.result()
                if r.get("error"):
                    raise ValueError(r["error"])
                out[t] = {
                    "start_price": round(r["cur"], 2),
                    "step": round(_step(t), 4),
                    "fresh": r["buys"],
                    # frozen copy of the live ladder at the same moment
                    "live": live[t].get("buys", []),
                    "live_source": live[t].get("source") or "hand",
                    "live_updated": live[t].get("updated"),
                }
            except Exception as e:
                failed.append((t, str(e)[:40]))
            if n % 50 == 0:
                print(f"  ... {n}/{len(tickers)}")

    SHADOW_FILE.write_text(json.dumps(
        {"generated": date.today().isoformat(), "tickers": out}, indent=1),
        encoding="utf-8")
    print(f"\n  Saved {len(out)} shadow ladders to {SHADOW_FILE.name}"
          f"  ({len(failed)} skipped)")
    for t, why in failed[:10]:
        print(f"    skipped {t}: {why}")

    # How different are the two sets, really?
    same = total = 0
    for d in out.values():
        for f in d["fresh"]:
            total += 1
            if any(abs(f["price"] - l["price"]) / f["price"] <= d["step"] / 2
                   for l in d["live"]):
                same += 1
    print(f"  {same} of {total} fresh rungs sit within half a step of a live rung"
          f" - the other {total - same} are where the sets actually differ.")


def score_rung(level: float, step: float, bars) -> tuple:
    """Return (outcome, days_to_resolve) for one buy rung over daily bars."""
    touched_at = None
    for i, (low, high) in enumerate(bars):
        if touched_at is None:
            if low > level:
                continue
            touched_at = i
            # the touch day itself can already have broken through
            if low <= level * (1 - step):
                return "broke", 0
            continue
        broke = low <= level * (1 - step)
        held = high >= level * (1 + step)
        if broke:  # ties go against the level
            return "broke", i - touched_at
        if held:
            return "held", i - touched_at
    return ("pending", None) if touched_at is not None else ("untouched", None)


def compare() -> None:
    if not SHADOW_FILE.exists():
        print("  No shadow set yet - run: python shadow_ladders.py generate")
        return
    shadow = json.loads(SHADOW_FILE.read_text(encoding="utf-8"))
    start = date.fromisoformat(shadow["generated"])
    tickers = shadow["tickers"]
    print(f"  Scoring {len(tickers)} tickers since {start}...")

    def bars_for(t):
        df = fetch_stock_data(t, period="1y")
        df = df[[d.date() > start for d in df.index]]
        return list(zip(df["Low"], df["High"]))

    bars = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(bars_for, t): t for t in tickers}
        for fut in as_completed(futures):
            try:
                bars[futures[fut]] = fut.result()
            except Exception:
                pass

    days = max((len(b) for b in bars.values()), default=0)
    # results[set][rung] -> Counter of outcomes
    results = {"live": {}, "fresh": {}}
    by_source = {}
    for t, d in tickers.items():
        b = bars.get(t)
        if b is None:
            continue  # price fetch failed - leave the ticker out entirely
        for which in ("live", "fresh"):
            for rung in d[which]:
                if rung["price"] >= d["start_price"]:
                    continue  # already through when the race began
                outcome, _ = score_rung(rung["price"], d["step"], b)
                bucket = results[which].setdefault(rung["rating"], {})
                bucket[outcome] = bucket.get(outcome, 0) + 1
                if which == "live":
                    src = by_source.setdefault(d["live_source"], {})
                    src[outcome] = src.get(outcome, 0) + 1

    def row(label, c):
        n = sum(c.values())
        touched = n - c.get("untouched", 0)
        held, broke = c.get("held", 0), c.get("broke", 0)
        rate = f"{held / (held + broke):5.0%}" if held + broke else "    -"
        return (f"    {label:<16} {n:5d}  {touched:7d}  {held:5d}  {broke:5d}"
                f"  {c.get('pending', 0):7d}   {rate}")

    def total(sets):
        out = {}
        for c in sets.values():
            for k, v in c.items():
                out[k] = out.get(k, 0) + v
        return out

    head = ("    {:<16} {:>5}  {:>7}  {:>5}  {:>5}  {:>7}   {}"
            .format("", "rungs", "touched", "held", "broke", "pending", "hold rate"))
    print(f"\n  LIVE vs FRESH LADDERS - {days} trading day(s) since {start}\n")
    print(head)
    print(row("live (Aug-ish)", total(results["live"])))
    print(row("fresh", total(results["fresh"])))

    print("\n  BY RUNG\n")
    print(head)
    for r in sorted(set(results["live"]) | set(results["fresh"])):
        for which in ("live", "fresh"):
            print(row(f"{which} rung {r}", results[which].get(r, {})))

    print("\n  LIVE SET BY WHO MADE IT\n")
    print(head)
    for src, c in sorted(by_source.items()):
        print(row(src, c))

    resolved = sum(total(results[w]).get(k, 0)
                   for w in results for k in ("held", "broke"))
    if resolved < 60:
        print(f"\n  Only {resolved} rungs resolved so far - too few to call. "
              f"Give it a few more weeks.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "generate":
        generate()
    elif cmd == "compare":
        compare()
    else:
        print(__doc__)
