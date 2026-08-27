"""Remember what was already reported, so the report can flag what is new.

A ladder that has been sitting at rung 2 for a week is not news. A ladder that
just went from rung 2 to rung 3 is - it means price dropped through a level and
the commitment called for has increased.

State is the deepest rung reported per ticker per side. A ticker that stops
triggering is dropped, so if it comes back later it counts as new again.
"""

import json
from datetime import date

from .constants import ROOT_DIR

ALERT_STATE_FILE = ROOT_DIR / ".alert_state.json"


def load_state() -> dict:
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict):
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_escalations(alerts: list, state: dict, side: str, seed: bool) -> list:
    """Tag alerts that are new or deeper than last time.

    On the very first run there is no history, so nothing is marked - otherwise
    every line would carry the flag and it would mean nothing.
    """
    prev = state.get(side, {})
    for a in alerts:
        was = prev.get(a["ticker"])
        a["escalated"] = (not seed) and (was is None or a["rating"] > was)
        a["prev_rung"] = was
    return alerts


def record(alerts: list, state: dict, side: str):
    """Replace stored state for this side with what is being reported now."""
    state[side] = {a["ticker"]: a["rating"] for a in alerts}
    state["updated"] = date.today().isoformat()
