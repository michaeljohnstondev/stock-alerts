"""Snooze a whole sector or industry from the buy side of the report.

    python snooze.py groups                    # every sector/industry on your list
    python snooze.py groups build              # ...filtered by name
    python snooze.py industry "Aerospace & Defense" 30 --why "war ended, expect pullback"
    python snooze.py sector "Real Estate" 30 --why "..."
    python snooze.py list                      # what is snoozed, until when, why
    python snooze.py history                   # every past snooze and its reason
    python snooze.py remove "Aerospace & Defense"

--why is required: a snooze with no reason is impossible to review later.
Days default to 30. Snoozes expire on their own and move to the history.
Names are matched without regard to case; use `groups` to find the exact one.
"""

import json
import sys
from collections import Counter

from stock_checker.constants import USER_RATINGS_FILE
from stock_checker.data import get_ticker_info
from stock_checker.group_snooze import (LEVELS, SNOOZE_DAYS_DEFAULT,
                                        expired_history, load_snoozes,
                                        snooze, unsnooze)


def _groups() -> dict:
    """{level: Counter(name -> tickers)} over every rated ticker."""
    tickers = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
    out = {lvl: Counter() for lvl in LEVELS}
    members = {}
    for t in tickers:
        info = get_ticker_info(t) or {}
        for lvl in LEVELS:
            name = info.get(lvl)
            if name and name != "ETF":
                out[lvl][name] += 1
                members.setdefault(name, []).append(t)
    return out, members


def cmd_groups(filt: str = ""):
    out, members = _groups()
    for lvl in LEVELS:
        rows = [(n, c) for n, c in out[lvl].most_common()
                if filt.lower() in n.lower()]
        if not rows:
            continue
        print(f"\n  {lvl.upper()}")
        for n, c in rows:
            sample = ", ".join(sorted(members[n])[:6])
            print(f"    {n:<40} {c:3d}   {sample}{' ...' if c > 6 else ''}")
    print()


def cmd_snooze(level: str, name: str, days: int, reason: str):
    out, members = _groups()
    match = next((n for n in out[level] if n.lower() == name.lower()), None)
    if not match:
        close = [n for n in out[level] if name.lower() in n.lower()]
        print(f"  No {level} named '{name}' on your list.")
        if close:
            print("  Did you mean: " + " | ".join(close))
        else:
            print("  See every name with: python snooze.py groups")
        return
    entry = snooze(level, match, days, reason)
    print(f"  Snoozed {level} '{match}' until {entry['until']} - "
          f"{len(members[match])} stocks hidden from buys: "
          f"{', '.join(sorted(members[match]))}")


def cmd_list():
    snoozes = load_snoozes()
    if not any(snoozes.values()):
        print("  Nothing snoozed.")
        return
    for lvl in LEVELS:
        for name, v in sorted(snoozes[lvl].items()):
            print(f"  {lvl:<9} {name:<32} until {v['until']}  (since {v['since']})")
            print(f"            why: {v.get('reason') or '-'}")


def cmd_history():
    past = expired_history()
    if not past:
        print("  No past snoozes.")
        return
    for e in sorted(past, key=lambda e: e["since"]):
        print(f"  {e['since']} -> {e['ended']:<24} {e['level']:<9} {e['name']}")
        print(f"            why: {e.get('reason') or '-'}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
    elif args[0] == "groups":
        cmd_groups(" ".join(args[1:]))
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "history":
        cmd_history()
    elif args[0] == "remove" and len(args) > 1:
        name = " ".join(args[1:])
        print(f"  Removed '{name}'." if unsnooze(name) else f"  '{name}' was not snoozed.")
    elif args[0] in LEVELS and len(args) > 1:
        rest = args[1:]
        reason = ""
        if "--why" in rest:
            i = rest.index("--why")
            reason = " ".join(rest[i + 1:]).strip()
            rest = rest[:i]
        if not reason:
            print('  Add a reason: --why "..." - e.g. --why "ran to all-time highs"')
            return
        days = SNOOZE_DAYS_DEFAULT
        if rest and rest[-1].isdigit():
            days = int(rest.pop())
        cmd_snooze(args[0], " ".join(rest), days, reason)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
