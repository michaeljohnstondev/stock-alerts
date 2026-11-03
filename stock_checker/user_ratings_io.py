"""Everything the report page reads besides the run itself, in one call."""

import json

from .constants import USER_RATINGS_FILE
from .holdings import load_holdings
from .notes import load_notes, is_stale


def load_all() -> tuple:
    """(ratings, notes, holdings, is_stale)"""
    ratings = json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8")) \
        if USER_RATINGS_FILE.exists() else {}
    return ratings, load_notes(), load_holdings(), is_stale
