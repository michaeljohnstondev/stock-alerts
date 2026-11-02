"""When a buy ladder is dead, and what to remember once it is replaced.

A ladder is dead when price has fallen through its deepest rung and stayed
there: the last CONFIRM_CLOSES closes all more than one ATR under the floor.
One ATR keeps a stock that is merely testing its floor - the moment the deep
rung is meant for - from being rolled; the run of closes keeps a single
flush day from doing it.

Rolling builds a fresh ladder below the new price and files the old one under
"broken", so the ladder number L says how many floors have already given way.
A rung 1 on L3 is not the same news as a rung 1 on L1.

Rung 5 is a watch price chosen on purpose and never counts as the floor or
gets rolled. Hand-set ladders are never rolled automatically either - they are
reported as stale for review.
"""

import numpy as np

CONFIRM_CLOSES = 3


def atr_pct(df, period: int = 14) -> float:
    """Average true range as a fraction of price - the stock's own scale."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev).abs(), (low - prev).abs()))
    atr = tr.tail(period).mean()
    return float(atr / close.iloc[-1])


def floor_rung(buys: list) -> float | None:
    """The deepest structure rung (1-4). None for a ladder with only a watch."""
    prices = [b["price"] for b in buys if b["rating"] != 5]
    return min(prices) if prices else None


def is_dead(buys: list, closes: list, atr: float) -> bool:
    floor = floor_rung(buys)
    if floor is None or len(closes) < CONFIRM_CLOSES or not atr:
        return False
    line = floor * (1 - atr)
    return all(c < line for c in closes[-CONFIRM_CLOSES:])


def ladder_number(entry: dict) -> int:
    return entry.get("ladder", 1)
