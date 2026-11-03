"""Research notes per ticker - what the company does, an AI impact grade, and
the long-term call. Written when you ask about a stock; read by the report.

Lives in state/ (gitignored) with the rest of your private data. Shape:
    {"TICKER": {"what": "...", "ai": "B+", "ai_why": "...",
                "outlook": "...", "updated": "YYYY-MM-DD", "sources": [...]}}

AI grade: will AI help or hurt this company's growth?
    A  direct tailwind     B  mostly helps     C  neutral
    D  real threat, but adapting               F  likely breaks the model
"""

import json
from datetime import date

from .constants import STATE_DIR

NOTES_FILE = STATE_DIR / "notes.json"
STALE_DAYS = 90   # a note older than this is worth a refresh


def load_notes() -> dict:
    if not NOTES_FILE.exists():
        return {}
    try:
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_note(ticker: str, **fields) -> dict:
    notes = load_notes()
    entry = notes.setdefault(ticker.upper(), {})
    entry.update({k: v for k, v in fields.items() if v is not None})
    entry["updated"] = date.today().isoformat()
    NOTES_FILE.write_text(json.dumps(notes, indent=2), encoding="utf-8")
    return entry


def is_stale(note: dict) -> bool:
    try:
        return (date.today() - date.fromisoformat(note["updated"])).days > STALE_DAYS
    except Exception:
        return True
