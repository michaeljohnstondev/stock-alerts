"""What you hold - quantity and cost basis per share.

Lives in state/ (gitignored) because positions and cost basis are private and
this repo is public. Shape:
    {"as_of": "YYYY-MM-DD", "positions": {"TICKER": [qty, basis], ...}}

Refreshed from a broker screenshot; a ticker dropped from it is a ticker sold.
"""

import json

from .constants import STATE_DIR

HOLDINGS_FILE = STATE_DIR / "holdings.json"


def load_holdings() -> dict:
    if not HOLDINGS_FILE.exists():
        return {}
    try:
        return json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_held() -> dict:
    """{ticker: (qty, basis)}"""
    return {t: tuple(v) for t, v in load_holdings().get("positions", {}).items()}
