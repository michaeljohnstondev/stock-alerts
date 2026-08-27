"""Generate sell (resistance) ladders and write them to .user_ratings.json.

The mirror of fill_levels. Where a buy ladder looks for support below spot -
shelves, unfilled gap-ups, pivot lows, rising MAs climbing to meet a falling
price - a sell ladder looks for resistance above it:

  - overhead shelves    (time spent at price - supply waiting to be sold)
  - pivot highs         (confirmed swing resistance)
  - unfilled gap-downs  (the drop nobody has bought back through)
  - falling MAs         (a declining average descending onto a rising price)

Cost basis is reported alongside, so each rung shows the gain it represents
rather than just a price.

Usage:
    python fill_sells.py --dry AVGO NFLX      # show what it would write
    python fill_sells.py AVGO NFLX            # write them
    python fill_sells.py --held               # every ticker in HELD below
"""

import json
import sys
from datetime import date

import numpy as np

from stock_checker.constants import USER_RATINGS_FILE
from stock_checker.data import fetch_stock_data, get_live_price
from suggest_levels import STRUCTURE_DAYS, _tick, _nice, price_profile
from fill_levels import _atr_pct, MIN_STEP_FLOOR, MIN_STEP_CAP

RUNGS = 4
BELOW_SPOT = 0.97       # resistance just underfoot still counts
WEIGHTS = {"shelf": 3.0, "gap": 3.0, "pivot": 2.0, "ma": 1.5}
CONFLUENCE_TOL = 0.02

# quantity, cost basis per share - for reporting gain at each rung
HELD = {
    "AEP": (9, 122.14), "ALLY": (7, 43.19), "AME": (2, 240.78),
    "AVGO": (4, 366.62), "BLDR": (5, 70.87), "CRWD": (10, 189.92),
    "DTE": (4, 135.44), "FIS": (1, 40.41), "FISV": (3, 51.27),
    "HALO": (1, 69.51), "HBAN": (2, 16.60), "HD": (1, 336.88),
    "KTOS": (2, 54.20), "LEN": (16, 102.40), "LHX": (4, 266.43),
    "LULU": (1, 120.03), "MAR": (1, 360.52), "MCHP": (22, 77.10),
    "NCLH": (10, 16.89), "NFLX": (20, 81.78), "NXPI": (10, 232.62),
    "ON": (4, 78.25), "ORCL": (1, 144.11), "PNC": (1, 245.89),
    "RH": (7, 166.79), "SBAC": (1, 180.13), "TXN": (1, 272.09),
    "UAL": (2, 116.53), "USB": (3, 62.82), "WFC": (4, 83.82),
    "WMT": (4, 102.40), "ZG": (45, 37.32),
}


def find_pivot_highs(df, window: int = 10) -> list:
    """Highs that are the highest point of a +/- window-day span."""
    high = df["High"].values
    out = []
    for i in range(window, len(high) - window):
        if high[i] == high[i - window:i + window + 1].max():
            out.append({"level": float(high[i]), "date": df.index[i].date(),
                        "age": len(high) - i})
    return out


def find_unfilled_gap_downs(df, min_pct: float = 0.015) -> list:
    """Gap-downs whose upper edge price has not traded back through.

    A gap down that never got bought back is overhead supply: everyone who
    owned it above that level is still underwater there.
    """
    close, open_, high = df["Close"].values, df["Open"].values, df["High"].values
    out = []
    for i in range(1, len(df)):
        prev_close = close[i - 1]
        if open_[i] < prev_close * (1 - min_pct):
            if high[i:].max() < prev_close:
                out.append({"level": float(prev_close), "bottom": float(open_[i]),
                            "date": df.index[i].date()})
    return out


def find_overhead_shelves(df, cur: float) -> list:
    """Bands above spot where price spent time - supply parked overhead."""
    band = max(_tick(cur), cur * 0.015)
    prof = price_profile(df, band)
    above = [b for b in prof if b["level"] > cur * BELOW_SPOT]
    if not above:
        return []
    peak = max(b["days"] for b in above)
    for b in above:
        b["strength"] = b["days"] / peak
    return above


def falling_mas(df) -> dict:
    """MAs that are declining - they descend onto a rising price."""
    close = df["Close"]
    specs = {"50 SMA": close.rolling(50).mean(),
             "200 SMA": close.rolling(200).mean(),
             "50 EMA": close.ewm(span=50, adjust=False).mean(),
             "200 EMA": close.ewm(span=200, adjust=False).mean()}
    out = {}
    for name, s in specs.items():
        s = s.dropna()
        if len(s) < 22:
            continue
        slope = (s.iloc[-1] - s.iloc[-22]) / 21
        out[name] = {"now": float(s.iloc[-1]), "slope": float(slope)}
    return out


def build_resistance(cur, shelves, pivots, gaps, mas) -> list:
    raw = []
    for s in shelves:
        if s["level"] > cur * BELOW_SPOT and s["strength"] >= 0.30:
            raw.append({"level": s["level"], "kind": "shelf",
                        "weight": WEIGHTS["shelf"] * s["strength"],
                        "note": "%dd at $%.2f-%.2f" % (s["days"], s["lo"], s["hi"])})
    for p in pivots:
        if p["level"] > cur * BELOW_SPOT:
            recency = max(0.4, 1 - p["age"] / 504)
            raw.append({"level": p["level"], "kind": "pivot",
                        "weight": WEIGHTS["pivot"] * recency,
                        "note": "pivot high %s" % p["date"]})
    for g in gaps:
        if g["level"] > cur * BELOW_SPOT:
            raw.append({"level": g["level"], "kind": "gap",
                        "weight": WEIGHTS["gap"],
                        "note": "unfilled gap down %s ($%.2f)" % (g["date"], g["level"])})
    for name, m in mas.items():
        if m["slope"] < 0 and m["now"] > cur * BELOW_SPOT:
            raw.append({"level": m["now"], "kind": "ma",
                        "weight": WEIGHTS["ma"], "note": "%s (falling)" % name})

    raw.sort(key=lambda r: r["level"])
    zones = []
    for r in raw:
        for z in zones:
            if abs(r["level"] - z["level"]) / cur <= CONFLUENCE_TOL:
                z["members"].append(r)
                z["level"] = sum(m["level"] for m in z["members"]) / len(z["members"])
                break
        else:
            zones.append({"level": r["level"], "members": [r]})
    for z in zones:
        z["raw"] = sum(m["weight"] for m in z["members"])
        z["kinds"] = set(m["kind"] for m in z["members"])
    return zones


def ladder_for(ticker: str) -> dict:
    df = fetch_stock_data(ticker, period="3y")
    if df is None or df.empty or len(df) < 220:
        return {"ticker": ticker, "error": "thin history"}
    cur = get_live_price(ticker) or float(df["Close"].iloc[-1])
    recent = df.tail(STRUCTURE_DAYS)
    zones = build_resistance(cur, find_overhead_shelves(recent, cur),
                             find_pivot_highs(recent),
                             find_unfilled_gap_downs(recent), falling_mas(df))
    if not zones:
        return {"ticker": ticker, "error": "no resistance found"}

    step = min(MIN_STEP_CAP, max(MIN_STEP_FLOOR, _atr_pct(df) * 1.5))
    best = max(z["raw"] for z in zones)
    # Only zones ABOVE spot can be sell rungs. Filtering after picking meant
    # four sub-spot zones got chosen and then all discarded, reporting "no
    # level above spot" on charts with plenty of overhead supply.
    live = sorted([z for z in zones if z["raw"] >= best * 0.15 and z["level"] > cur],
                  key=lambda z: z["level"])
    picked = []
    for z in live:
        if picked and z["level"] < picked[-1]["level"] * (1 + step):
            continue
        picked.append(z)
        if len(picked) == RUNGS:
            break

    sells, seen = [], set()
    for z in picked:
        price = _nice(z["level"], cur)
        if price in seen or price <= cur:
            continue
        seen.add(price)
        sells.append({"price": price, "rating": len(sells) + 1})
    if not sells:
        return {"ticker": ticker, "error": "no level above spot"}
    return {"ticker": ticker, "cur": cur, "sells": sells,
            "kinds": [sorted(z["kinds"]) for z in picked]}


def main():
    args = list(sys.argv[1:])
    dry = "--dry" in args
    if dry:
        args.remove("--dry")
    if "--held" in args:
        args.remove("--held")
        args = list(HELD)
    if not args:
        print(__doc__)
        return

    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    for ticker in [a.upper() for a in args]:
        try:
            r = ladder_for(ticker)
        except Exception as e:
            r = {"ticker": ticker, "error": str(e)[:50]}
        if r.get("error"):
            print("  %-6s  --  %s" % (ticker, r["error"]))
            continue

        qty, basis = HELD.get(ticker, (0, 0))
        parts = []
        for s in r["sells"]:
            gain = (s["price"] / basis - 1) * 100 if basis else 0
            parts.append("$%.2f(%d)%s" % (s["price"], s["rating"],
                                          " %+.0f%%" % gain if basis else ""))
        print("  %-6s $%-9.2f basis $%-8.2f  %s" % (ticker, r["cur"], basis, "  ".join(parts)))
        if not dry:
            entry = ratings.setdefault(ticker, {})
            entry["sells"] = r["sells"]
            # tracked apart from "source" so an auto sell ladder does not
            # mislabel a hand-set buy ladder as auto
            entry["sells_source"] = "auto"
            entry["updated"] = date.today().isoformat()
            USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
