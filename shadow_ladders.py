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

Not every rung is a horizontal line. Each one is typed by the structure
under it:

    horizontal  shelves, pivot lows, unfilled gaps - a fixed price
    ma          a rising moving average, projected forward - a sloped line
    mixed       both at the same price (confluence)
    unmatched   live rungs only - no structure the scanner can find today
                sits under it, so it is your own read (trendlines included)

A projected MA rung is stored as a single future value of a moving line,
so a fixed-price test grades it on the wrong number most days. Fresh MA
and mixed rungs are scored twice: frozen at the stored price (what an
alert does today) and following the real MA day by day (what the line
does). If the second holds clearly better, rungs that track their MA are
worth building.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from stock_checker.constants import ROOT_DIR, USER_RATINGS_FILE
from stock_checker.data import fetch_stock_data
from fill_levels import ladder_for, _atr_pct, MIN_STEP_FLOOR, MIN_STEP_CAP

SHADOW_FILE = ROOT_DIR / ".shadow_ladders.json"


# Same four lines suggest_levels.project_mas projects from
MA_SPECS = {
    "50 SMA": lambda c: c.rolling(50).mean(),
    "200 SMA": lambda c: c.rolling(200).mean(),
    "50 EMA": lambda c: c.ewm(span=50, adjust=False).mean(),
    "200 EMA": lambda c: c.ewm(span=200, adjust=False).mean(),
}


def _zone_type(zone: dict) -> tuple:
    """(type, ma_name) for a suggest_levels zone."""
    kinds = {m["kind"] for m in zone["members"]}
    ma = next((m["note"].split(" in ")[0] for m in zone["members"]
               if m["kind"] == "ma"), None)
    if kinds == {"ma"}:
        return "ma", ma
    return ("mixed" if "ma" in kinds else "horizontal"), ma


def _label_live(price: float, zones: list, step: float) -> str:
    """Type a live rung by the nearest structure within half a step."""
    near = [z for z in zones if abs(z["level"] - price) / price <= step / 2]
    if not near:
        return "unmatched"
    z = min(near, key=lambda z: abs(z["level"] - price))
    return _zone_type(z)[0]


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
                step = _step(t)
                fresh = []
                for b, z in zip(r["buys"], r["zones"]):
                    kind, ma = _zone_type(z)
                    fresh.append({**b, "type": kind, "ma": ma})
                out[t] = {
                    "start_price": round(r["cur"], 2),
                    "step": round(step, 4),
                    "fresh": fresh,
                    # frozen copy of the live ladder at the same moment
                    "live": [{**b, "type": _label_live(b["price"], r["all_zones"], step)}
                             for b in live[t].get("buys", [])],
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


def score_rung(level, step: float, bars) -> tuple:
    """Return (outcome, days_to_resolve) for one buy rung over daily bars.

    level is a price, or a list of one price per bar for a rung that moves.
    """
    touched_at = None
    for i, (low, high) in enumerate(bars):
        lvl = level[i] if isinstance(level, list) else level
        if touched_at is None:
            if low > lvl:
                continue
            touched_at = i
            # the touch day itself can already have broken through
            if low <= lvl * (1 - step):
                return "broke", 0
            continue
        broke = low <= lvl * (1 - step)
        held = high >= lvl * (1 + step)
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
        # 3y so the 200-day lines are computed on the same history as when
        # the rungs were generated
        df = fetch_stock_data(t, period="3y")
        after = [d.date() > start for d in df.index]
        lines = {name: [float(v) for v in f(df["Close"])[after]]
                 for name, f in MA_SPECS.items()}
        df = df[after]
        return list(zip(df["Low"], df["High"])), lines

    bars = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(bars_for, t): t for t in tickers}
        for fut in as_completed(futures):
            try:
                bars[futures[fut]] = fut.result()
            except Exception:
                pass

    days = max((len(b) for b, _ in bars.values()), default=0)
    def tally(table, key, outcome):
        c = table.setdefault(key, {})
        c[outcome] = c.get(outcome, 0) + 1

    # results[set][rung] -> Counter of outcomes
    results = {"live": {}, "fresh": {}}
    by_source, by_type = {}, {}
    for t, d in tickers.items():
        got = bars.get(t)
        if got is None:
            continue  # price fetch failed - leave the ticker out entirely
        b, lines = got
        for which in ("live", "fresh"):
            for rung in d[which]:
                if rung["price"] >= d["start_price"]:
                    continue  # already through when the race began
                outcome, _ = score_rung(rung["price"], d["step"], b)
                tally(results[which], rung["rating"], outcome)
                tally(by_type, f"{which} {rung['type']}", outcome)
                if which == "live":
                    tally(by_source, d["live_source"], outcome)
                elif rung["type"] in ("ma", "mixed") and rung.get("ma") in lines:
                    # Mixed rungs are scored along their MA too: a horizontal
                    # level and a rising line meet once, then part ways.
                    moving, _ = score_rung(lines[rung["ma"]], d["step"], b)
                    tally(by_type, f"fresh {rung['type']}, follows", moving)

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

    print("\n  BY LEVEL TYPE  (ma = frozen price, 'follows' = scored on the real line)\n")
    print(head)
    for which in ("live", "fresh"):
        for kind in ("horizontal", "ma", "ma, follows", "mixed", "mixed, follows",
                     "unmatched"):
            key = f"{which} {kind}"
            if key in by_type:
                print(row(key, by_type[key]))

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
