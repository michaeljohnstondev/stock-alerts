"""Muted alerts - you saw it, you passed, stop showing it for a while.

Distinct from the skip/snooze in rate_stocks.py, which pauses RE-RATING a
ticker. This pauses REPORTING a specific rung you have already decided against.

Muting is per (ticker, rung) on purpose. Passing on rung 2 says nothing about
rung 3 - a deeper rung is a genuinely different proposition, and burying it
would defeat the point of a ladder.
"""

import json
from datetime import date, timedelta

from .constants import ROOT_DIR

MUTED_FILE = ROOT_DIR / ".muted.json"
MUTE_DAYS_DEFAULT = 14


def load_mutes() -> dict:
    """Live mutes, with anything past its date already dropped."""
    if not MUTED_FILE.exists():
        return {}
    try:
        raw = json.loads(MUTED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    today = date.today().isoformat()
    live = {}
    for ticker, rungs in raw.items():
        keep = {r: v for r, v in rungs.items() if v.get("until", "") > today}
        if keep:
            live[ticker] = keep
    if live != raw:
        save_mutes(live)
    return live


def save_mutes(mutes: dict):
    MUTED_FILE.write_text(json.dumps(mutes, indent=2), encoding="utf-8")


def add_mute(mutes: dict, ticker: str, rung, side: str = "buy",
             days: int = MUTE_DAYS_DEFAULT, note: str = "") -> str:
    """Mute one rung, or every rung of a ticker when rung is None."""
    until = (date.today() + timedelta(days=days)).isoformat()
    key = "%s%s" % (side[0], rung if rung is not None else "*")
    entry = mutes.setdefault(ticker, {})
    entry[key] = {"since": date.today().isoformat(), "until": until}
    if note:
        entry[key]["note"] = note
    return until


def is_muted(mutes: dict, ticker: str, rung: int, side: str = "buy") -> bool:
    entry = mutes.get(ticker)
    if not entry:
        return False
    p = side[0]
    return ("%s%d" % (p, rung)) in entry or ("%s*" % p) in entry


def show_mutes(mutes: dict):
    if not mutes:
        print("\n  Nothing muted.\n")
        return
    print("\n  %-8s  %-6s  %-12s  %-12s  %s" % ("TICKER", "RUNG", "MUTED", "RETURNS", "NOTE"))
    print("  %-8s  %-6s  %-12s  %-12s  %s" % ("------", "----", "-----", "-------", "----"))
    n = 0
    for ticker in sorted(mutes):
        for key, v in sorted(mutes[ticker].items()):
            side = "buy" if key[0] == "b" else "sell"
            rung = key[1:]
            label = "%s %s" % (side, "all" if rung == "*" else rung)
            print("  %-8s  %-6s  %-12s  %-12s  %s"
                  % (ticker, label, v.get("since", "-"), v.get("until", "-"), v.get("note", "")))
            n += 1
    print("\n  %d muted.\n" % n)
