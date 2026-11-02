"""Snoozed sectors and industries - stay away from a whole group for a while.

Distinct from the per-ticker skip in rate_stocks.py and the per-rung mutes
in mutes.py. This is for deciding ahead of time that you are done with an
area - homebuilders after a bad quarter, say - and not seeing its buy
alerts tempt you back in until the date you chose.

Buy alerts only. Sell alerts still show: if you already hold something in
a snoozed group, its exits matter more, not less.

Builders are an INDUSTRY ("Residential Construction") inside the Consumer
Cyclical SECTOR, next to Amazon and Tesla. So snoozes work at either level.
"""

import json
from datetime import date, timedelta

from .constants import ROOT_DIR
from .data import get_ticker_info

SNOOZED_GROUPS_FILE = ROOT_DIR / ".snoozed_groups.json"
SNOOZE_DAYS_DEFAULT = 30
LEVELS = ("sector", "industry")


def load_snoozes() -> dict:
    """{"sector": {name: {since, until}}, "industry": {...}}, expired dropped."""
    empty = {lvl: {} for lvl in LEVELS}
    if not SNOOZED_GROUPS_FILE.exists():
        return empty
    try:
        raw = json.loads(SNOOZED_GROUPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return empty
    today = date.today().isoformat()
    live = {lvl: {name: v for name, v in raw.get(lvl, {}).items()
                  if v.get("until", "") > today}
            for lvl in LEVELS}
    if live != raw:
        save_snoozes(live)
    return live


def save_snoozes(snoozes: dict):
    SNOOZED_GROUPS_FILE.write_text(json.dumps(snoozes, indent=2), encoding="utf-8")


def snooze(level: str, name: str, days: int = SNOOZE_DAYS_DEFAULT) -> dict:
    snoozes = load_snoozes()
    entry = {"since": date.today().isoformat(),
             "until": (date.today() + timedelta(days=days)).isoformat()}
    snoozes[level][name] = entry
    save_snoozes(snoozes)
    return entry


def unsnooze(name: str) -> bool:
    snoozes = load_snoozes()
    hit = False
    for lvl in LEVELS:
        for n in list(snoozes[lvl]):
            if n.lower() == name.lower():
                del snoozes[lvl][n]
                hit = True
    save_snoozes(snoozes)
    return hit


def snoozed_group(ticker: str, snoozes: dict) -> str | None:
    """The snoozed sector or industry this ticker belongs to, or None."""
    if not any(snoozes.values()):
        return None  # nothing snoozed - skip the lookup entirely
    info = get_ticker_info(ticker) or {}
    for lvl in LEVELS:
        if info.get(lvl) in snoozes[lvl]:
            return info[lvl]
    return None
