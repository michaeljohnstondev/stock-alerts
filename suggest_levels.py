"""Suggest buy-level candidates from chart structure.

Finds the levels a discretionary chart reader would mark, straight from OHLCV:
  - consolidation shelves  (time spent at price - bases get retested)
  - pivot lows             (confirmed swing support)
  - unfilled gaps          (launch points that never got filled)
  - projected MAs          (where a RISING 200/50 MA will be in 1-6 months)

Levels from different methods that land on the same price are scored as
confluence and ranked. Nothing is invented - output is candidates to pick from.

Usage:
    python suggest_levels.py HPE
    python suggest_levels.py HPE ORCL CRM      # several at once
    python suggest_levels.py HPE --all         # every candidate, not just top 8
    python suggest_levels.py HPE --profile     # raw time-at-price histogram
"""

import json
import sys

import numpy as np

from stock_checker.constants import USER_RATINGS_FILE
from stock_checker.data import fetch_stock_data, get_live_price

TRADING_DAYS_PER_MONTH = 21
PROJECT_MONTHS = (1, 2, 3, 6)

# How far back to hunt for shelves, pivots and gaps. MA slope still uses the
# full 3y fetch - it needs 200 bars before it produces a single value.
STRUCTURE_DAYS = 378  # ~18 months

# A 4th rung must clear the 3rd by at least this much to be worth placing
MIN_RUNG_GAP = 0.03

# How close two levels must be to count as the same level (fraction of price)
CONFLUENCE_TOL = 0.02

# Method weights - structure you can point to on a chart outranks a projection
WEIGHTS = {"shelf": 3.0, "gap": 3.0, "pivot": 2.0, "ma": 1.5}

# Structure just overhead still matters: after a dip, the shelf you fell out
# of is often where the first rung goes.
ABOVE_SPOT = 1.03

# A buy ladder lives in a band below spot. Levels far beyond it are real
# structure but not actionable entries, so they are faded, then cut.
DEPTH_SWEET_SPOT = 0.12   # no penalty out to here
DEPTH_MAX = 0.40          # dropped entirely past here

# How far forward each MA may credibly be projected. A 50-day MA's slope
# says nothing about where it sits in six months.
MA_MAX_MONTHS = {"50 SMA": 2, "50 EMA": 2, "200 SMA": 6, "200 EMA": 6}


def _tick(price: float) -> float:
    """Rounding increment that matches how a person writes a limit price."""
    if price >= 500:
        return 5.0
    if price >= 100:
        return 1.0
    if price >= 20:
        return 0.5   # on a $60 stock people say 61.5, not 61
    return 0.25


def _nice(level: float, ref: float) -> float:
    t = _tick(ref)
    return round(round(level / t) * t, 2)


def find_shelves(df, cur: float, lookback: int = 252) -> list:
    """Bands where price spent the most time. Bases get retested."""
    recent = df.tail(lookback)
    band = max(_tick(cur), cur * 0.015)
    lo = np.floor(recent["Low"].min() / band) * band
    hi = recent["High"].max()
    edges = np.arange(lo, hi + band, band)

    # Recent days count for more: in a stock that has run hard, the bands it
    # camped in a year ago are not support any more.
    n = len(recent)
    recency = np.linspace(0.25, 1.0, n)

    out = []
    for i in range(len(edges) - 1):
        b_lo, b_hi = edges[i], edges[i + 1]
        # A day "occupies" a band if its range overlaps it
        hit = ((recent["Low"] <= b_hi) & (recent["High"] >= b_lo)).values
        days = int(hit.sum())
        if days:
            out.append({"level": (b_lo + b_hi) / 2, "days": days,
                        "weighted": float((hit * recency).sum()),
                        "lo": b_lo, "hi": b_hi})
    if not out:
        return []

    peak = max(s["weighted"] for s in out)
    for s in out:
        s["strength"] = s["weighted"] / peak
    return out


def find_pivot_lows(df, window: int = 10) -> list:
    """Lows that are the lowest point of a +/- window-day span."""
    low = df["Low"].values
    out = []
    for i in range(window, len(low) - window):
        if low[i] == low[i - window:i + window + 1].min():
            out.append({"level": float(low[i]),
                        "date": df.index[i].date(),
                        "age": len(low) - i})
    return out


def find_unfilled_gaps(df, min_pct: float = 0.015) -> list:
    """Gap-ups whose lower edge price has not traded back through."""
    close = df["Close"].values
    open_ = df["Open"].values
    low = df["Low"].values
    out = []
    for i in range(1, len(df)):
        prev_close = close[i - 1]
        if open_[i] > prev_close * (1 + min_pct):
            # Filled if any later low reached back down to the pre-gap close
            if low[i:].min() > prev_close:
                out.append({"level": float(prev_close),
                            "top": float(open_[i]),
                            "date": df.index[i].date(),
                            "size_pct": (open_[i] / prev_close - 1) * 100})
    return out


def project_mas(df) -> dict:
    """Current and forward-projected MAs, using each one's recent slope.

    A rising MA climbs to meet a falling price - where they intersect is a
    level that does not exist on today's chart.
    """
    close = df["Close"]
    specs = {
        "50 SMA": close.rolling(50).mean(),
        "200 SMA": close.rolling(200).mean(),
        "50 EMA": close.ewm(span=50, adjust=False).mean(),
        "200 EMA": close.ewm(span=200, adjust=False).mean(),
    }
    out = {}
    for name, series in specs.items():
        s = series.dropna()
        if len(s) < TRADING_DAYS_PER_MONTH + 1:
            continue
        slope = (s.iloc[-1] - s.iloc[-1 - TRADING_DAYS_PER_MONTH]) / TRADING_DAYS_PER_MONTH
        out[name] = {
            "now": float(s.iloc[-1]),
            "slope": float(slope),
            "proj": {m: float(s.iloc[-1] + slope * m * TRADING_DAYS_PER_MONTH)
                     for m in PROJECT_MONTHS},
        }
    return out


def build_candidates(cur, shelves, pivots, gaps, mas) -> list:
    """Collect every below-market level, then cluster into confluence zones."""
    raw = []

    for s in shelves:
        if s["level"] < cur * ABOVE_SPOT and s["strength"] >= 0.35:
            # Sub-dollar bands need a decimal or the label reads "$63-63"
            fmt = "%dd at $%.1f-%.1f" if (s["hi"] - s["lo"]) < 1 else "%dd at $%.0f-%.0f"
            raw.append({"level": s["level"], "kind": "shelf",
                        "weight": WEIGHTS["shelf"] * s["strength"],
                        "note": fmt % (s["days"], s["lo"], s["hi"])})

    for p in pivots:
        if p["level"] < cur * ABOVE_SPOT:
            # Recent pivots matter more than year-old ones
            recency = max(0.4, 1 - p["age"] / 504)
            raw.append({"level": p["level"], "kind": "pivot",
                        "weight": WEIGHTS["pivot"] * recency,
                        "note": "pivot low %s" % p["date"]})

    for g in gaps:
        if g["level"] < cur * ABOVE_SPOT:
            raw.append({"level": g["level"], "kind": "gap",
                        "weight": WEIGHTS["gap"],
                        "note": "unfilled gap %s ($%.2f -> $%.2f)"
                                % (g["date"], g["level"], g["top"])})

    for name, m in mas.items():
        if m["slope"] <= 0:
            continue  # a falling MA offers no forward convergence
        for months, val in m["proj"].items():
            if months > MA_MAX_MONTHS.get(name, 3):
                continue
            if val < cur * ABOVE_SPOT:
                raw.append({"level": val, "kind": "ma",
                            "weight": WEIGHTS["ma"],
                            "note": "%s in %dmo" % (name, months)})

    # Cluster: walk levels high to low, merge anything within tolerance
    raw.sort(key=lambda r: -r["level"])
    zones = []
    for r in raw:
        for z in zones:
            if abs(r["level"] - z["level"]) / cur <= CONFLUENCE_TOL:
                z["members"].append(r)
                z["level"] = sum(m["level"] for m in z["members"]) / len(z["members"])
                break
        else:
            zones.append({"level": r["level"], "members": [r]})

    out = []
    for z in zones:
        depth = (cur - z["level"]) / cur
        if depth > DEPTH_MAX:
            continue
        # Full credit inside the sweet spot, then fade to zero at DEPTH_MAX
        if depth <= DEPTH_SWEET_SPOT:
            decay = 1.0
        else:
            decay = 1 - (depth - DEPTH_SWEET_SPOT) / (DEPTH_MAX - DEPTH_SWEET_SPOT)
        z["depth"] = depth
        z["score"] = sum(m["weight"] for m in z["members"]) * decay
        z["kinds"] = set(m["kind"] for m in z["members"])
        z["suggest"] = _nice(z["level"], cur)
        out.append(z)
    return sorted(out, key=lambda z: -z["score"])


def price_profile(df, band: float) -> list:
    """Raw time-at-price, NO recency weighting.

    Deliberately unweighted: the heaviest bases are often old, and that is
    exactly what a deep capitulation rung wants to sit on.
    """
    lo = np.floor(df["Low"].min() / band) * band
    hi = df["High"].max()
    edges = np.arange(lo, hi + band, band)
    out = []
    for i in range(len(edges) - 1):
        b_lo, b_hi = edges[i], edges[i + 1]
        days = int(((df["Low"] <= b_hi) & (df["High"] >= b_lo)).sum())
        if days:
            out.append({"level": (b_lo + b_hi) / 2, "days": days,
                        "lo": b_lo, "hi": b_hi})
    return out


def extend_ladder(buys, cur, df, span: float = 0.08) -> dict:
    """Project a 4th rung from the spacing of the existing rungs.

    Works in log space, so the ladder's widening is treated as a ratio
    rather than a dollar amount. The projection is then snapped to the
    heaviest real base near it - trend picks the zone, structure picks
    the price.
    """
    prices = [e["price"] for e in sorted(buys, key=lambda e: e["rating"])]
    if len(prices) < 2:
        return None

    drops = [np.log(prices[i] / prices[i + 1]) for i in range(len(prices) - 1)]
    if len(drops) >= 2 and drops[-2] > 0:
        ratio = drops[-1] / drops[-2]
    else:
        ratio = 1.5  # only one gap to go on - assume a typical widening
    clamped = not (1.0 <= ratio <= 2.5)
    ratio = min(2.5, max(1.0, ratio))

    raw = prices[-1] * np.exp(-drops[-1] * ratio)

    # Snap to the heaviest base near the projection. The window is measured
    # against the projection itself, and the result must clear the existing
    # deepest rung by a real margin - a 4th rung level with the 3rd is no
    # rung at all.
    ceiling = prices[-1] * (1 - MIN_RUNG_GAP)
    prof = price_profile(df, max(_tick(cur), cur * 0.015))
    near = [b for b in prof
            if abs(b["level"] - raw) / raw <= span and b["level"] < ceiling]
    best = max(near, key=lambda b: b["days"]) if near else None

    suggest = _nice(best["level"] if best else raw, cur)
    if suggest >= ceiling:  # rounding pushed it back into buy 3
        best = None
        suggest = _nice(min(raw, ceiling), cur)

    low = float(df["Low"].min())
    return {"raw": float(raw), "ratio": float(ratio), "clamped": clamped,
            "drops": [float(d) for d in drops], "best": best,
            "below_range": raw < low, "range_low": low, "suggest": suggest}


def entry_rung(df, cur: float) -> dict:
    """Which rung today's price already represents.

    Rung number means commitment, not depth. A name that has already fallen
    hard deserves a bigger starting commitment than one sitting at its highs,
    so the ladder should not always begin at 1 underneath spot.

    Read: how much of the stock's own traded history sits ABOVE the current
    price. Near the highs, almost none does and there is a full ladder below.
    Deep in an old base, most of it does - price has already made the move
    that rungs 2 and 3 were waiting for.
    """
    band = max(_tick(cur), cur * 0.015)
    prof = price_profile(df, band)
    total = sum(b["days"] for b in prof) or 1
    above = sum(b["days"] for b in prof if b["level"] > cur) / total

    hi, lo = float(df["High"].max()), float(df["Low"].min())
    pos = (cur - lo) / (hi - lo) if hi > lo else 0.5  # 0 = at lows, 1 = at highs

    score = (above + (1 - pos)) / 2
    if score >= 0.72:
        rung, why = 3, "deep in its own base - most history is overhead"

    elif score >= 0.52:
        rung, why = 2, "well off the highs, a good part of its history is overhead"
    else:
        rung, why = 1, "near the upper end of its range"
    return {"rung": rung, "above": above, "pos": pos, "score": score, "why": why}


def print_profile(df, cur: float, rated: dict = None):
    """Raw time-at-price histogram, unweighted.

    The ranked list fades old bases by design; this does not. For a deep
    rung it is the more honest view - a base from a year ago is still where
    the buyers were.
    """
    band = max(_tick(cur), cur * 0.015)
    prof = [b for b in price_profile(df, band) if b["level"] <= cur * 1.15]
    if not prof:
        return
    peak = max(b["days"] for b in prof)
    marks = {}
    for e in (rated or {}).get("buys", []):
        marks[round(e["price"] / band)] = e["rating"]

    print("\n  TIME AT PRICE  (unweighted, last %d months)" % (STRUCTURE_DAYS // 21))
    for b in sorted(prof, key=lambda b: -b["level"]):
        bar = "#" * int(b["days"] / peak * 34)
        here = "  <-- spot" if b["lo"] <= cur < b["hi"] else ""
        tag = marks.get(round(b["level"] / band))
        if tag:
            here += "  <-- your buy %d" % tag
        print("    $%-7.0f %-35s %3dd%s" % (b["lo"], bar, b["days"], here))


def report(ticker: str, show_all: bool = False, rated: dict = None,
           profile: bool = False):
    df = fetch_stock_data(ticker, period="3y")
    if df is None or df.empty or len(df) < 220:
        print("\n  %s: not enough history.\n" % ticker)
        return

    cur = get_live_price(ticker) or float(df["Close"].iloc[-1])
    # MA slopes need the long history; structure does not. A pivot low from
    # three years ago is not a level anyone is watching today.
    recent = df.tail(STRUCTURE_DAYS)
    shelves = find_shelves(recent, cur)
    pivots = find_pivot_lows(recent)
    gaps = find_unfilled_gaps(recent)
    mas = project_mas(df)
    zones = build_candidates(cur, shelves, pivots, gaps, mas)

    print()
    print("  " + "=" * 76)
    print("    %s   $%.2f" % (ticker, cur))
    print("  " + "=" * 76)

    if mas:
        print("\n  PROJECTED MOVING AVERAGES  (a rising MA climbs to meet a falling price)")
        head = "".join("%10s" % ("+%dmo" % m) for m in PROJECT_MONTHS)
        print("    %-9s%10s%s" % ("", "now", head))
        for name, m in mas.items():
            if m["slope"] <= 0:
                # A falling MA is overhead resistance, not rising support -
                # projecting it forward produces nonsense (and eventually $0).
                row = "%10s" % "falling" + "%10s" % "" * (len(PROJECT_MONTHS) - 1)
            else:
                row = "".join("%10s" % ("$%.2f" % m["proj"][x]) for x in PROJECT_MONTHS)
            print("    %-9s%10s%s" % (name, "$%.2f" % m["now"], row))

    if profile:
        print_profile(recent, cur, rated)

    print("\n  CANDIDATE LEVELS")
    shown = zones if show_all else zones[:8]
    if not shown:
        print("    none - price is at or below all identified structure.")
    for z in shown:
        pct = (cur - z["level"]) / cur * 100
        side = "below" if pct >= 0 else "ABOVE"
        stars = "*" * min(5, int(z["score"] / 2) + 1)
        conf = "  <-- CONFLUENCE" if len(z["kinds"]) >= 2 else ""
        print("\n    $%-9.2f %5.1f%% below   %-5s%s" % (z["suggest"], pct, stars, conf))
        for m in sorted(z["members"], key=lambda m: -m["weight"])[:4]:
            print("        %-6s %s" % (m["kind"], m["note"]))

    if rated and rated.get("buys"):
        print("\n  YOUR CURRENT LEVELS (%s)" % rated.get("updated", "-"))
        for e in sorted(rated["buys"], key=lambda e: e["rating"]):
            pct = (cur - e["price"]) / cur * 100
            tag = ""
            if zones:
                near = min(zones, key=lambda z: abs(z["level"] - e["price"]))
                if abs(near["level"] - e["price"]) / cur <= CONFLUENCE_TOL:
                    tag = "   ~ " + ", ".join(sorted(near["kinds"]))
            print("    buy %d   $%-9.2f %5.1f%% %-5s%s"
                  % (e["rating"], e["price"], abs(pct), "below" if pct >= 0 else "ABOVE", tag))

        # Full history, not the 18-month window: a capitulation rung wants the
        # old deep bases that recent structure does not reach.
        ext = extend_ladder(rated["buys"], cur, df)
        if ext:
            pct = (cur - ext["suggest"]) / cur * 100
            print("\n  PROJECTED BUY 4  (extends your own spacing, then snaps to structure)")
            steps = " -> ".join("%.1f%%" % (d * 100) for d in ext["drops"])
            print("    your steps      %s  then x%.2f" % (steps, ext["ratio"]))
            print("    trend projects  $%.2f" % ext["raw"])
            if ext["best"]:
                b = ext["best"]
                print("    nearest base    $%.0f-%.0f  (%dd at price)" % (b["lo"], b["hi"], b["days"]))
            else:
                print("    nearest base    none nearby - projection is in open space")
            print("    >> buy 4        $%-9.2f %5.1f%% below" % (ext["suggest"], pct))
            if ext["clamped"]:
                print("       note: your widening was outside 1.0-2.5x and was clamped")
            if ext["below_range"]:
                print("       note: BELOW the 18-month low of $%.2f - no structure exists there"
                      % ext["range_low"])
    print()


def load_ratings() -> dict:
    if USER_RATINGS_FILE.exists():
        try:
            return json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def main():
    args = list(sys.argv[1:])
    show_all = "--all" in args
    if show_all:
        args.remove("--all")
    profile = "--profile" in args
    if profile:
        args.remove("--profile")
    if not args:
        print(__doc__)
        return

    ratings = load_ratings()
    for ticker in [a.upper() for a in args]:
        try:
            report(ticker, show_all, ratings.get(ticker), profile)
        except Exception as e:
            print("\n  %s: failed - %s\n" % (ticker, e))


if __name__ == "__main__":
    main()
