"""Snoozed sectors and industries - stay away from a whole group for a while.

Distinct from the per-ticker skip in rate_stocks.py and the per-rung mutes
in mutes.py. This is for deciding ahead of time that a group's buy alerts
should not tempt you for a while. The good cases have a reason that runs
out: a group that has run to all-time highs, an event whose effect fades
(defense after a war ends), an area you just lost money in.

Buy alerts only. Sell alerts still show: if you already hold something in
a snoozed group, its exits matter more, not less.

Builders are an INDUSTRY ("Residential Construction") inside the Consumer
Cyclical SECTOR, next to Amazon and Tesla. So snoozes work at either level.

The file is meant to be read by a person or a fresh Claude session:

    {"sector":   {name: {since, until, reason}},
     "industry": {name: {since, until, reason}},
     "expired":  [{level, name, since, until, reason, ended}, ...]}

Every snooze carries the reason it was set, and nothing is deleted - an
expired or removed snooze moves to "expired", so the file is a log of what
was avoided, why, and when it ended.
"""

import json
from datetime import date, timedelta

from .constants import STATE_DIR
from .data import get_ticker_info

SNOOZED_GROUPS_FILE = STATE_DIR / "snoozed_groups.json"
SNOOZE_DAYS_DEFAULT = 30
LEVELS = ("sector", "industry")


def _read() -> dict:
    raw = {}
    if SNOOZED_GROUPS_FILE.exists():
        try:
            raw = json.loads(SNOOZED_GROUPS_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    for lvl in LEVELS:
        raw.setdefault(lvl, {})
    raw.setdefault("expired", [])
    return raw


def _write(raw: dict):
    SNOOZED_GROUPS_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _retire(raw: dict, lvl: str, name: str, ended: str):
    v = raw[lvl].pop(name)
    raw["expired"].append({"level": lvl, "name": name, **v, "ended": ended})


def load_snoozes() -> dict:
    """{"sector": {...}, "industry": {...}} - live only. Expired get logged."""
    raw = _read()
    today = date.today().isoformat()
    changed = False
    for lvl in LEVELS:
        for name, v in list(raw[lvl].items()):
            if v.get("until", "") <= today:
                _retire(raw, lvl, name, v.get("until", today))
                changed = True
    if changed:
        _write(raw)
    return {lvl: dict(raw[lvl]) for lvl in LEVELS}


def expired_history() -> list:
    load_snoozes()  # retire anything that lapsed since the last read
    return _read()["expired"]


def snooze(level: str, name: str, days: int, reason: str) -> dict:
    load_snoozes()
    raw = _read()
    entry = {"since": date.today().isoformat(),
             "until": (date.today() + timedelta(days=days)).isoformat(),
             "reason": reason}
    raw[level][name] = entry
    _write(raw)
    return entry


def unsnooze(name: str) -> bool:
    """End a snooze early. It moves to the log rather than vanishing."""
    raw = _read()
    hit = False
    for lvl in LEVELS:
        for n in list(raw[lvl]):
            if n.lower() == name.lower():
                _retire(raw, lvl, n, date.today().isoformat() + " (ended early)")
                hit = True
    if hit:
        _write(raw)
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
