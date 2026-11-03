"""Quotes from Schwab - the consolidated price across all exchanges, the same
source your fills come from. The first choice for any live price; the free
Alpaca feed only sees IEX (one small exchange) and can be off after hours.

Returns None when Schwab isn't logged in or doesn't answer, so callers fall
back to the other feeds.
"""

import threading

_client = None
_lock = threading.Lock()


def _get_client():
    global _client
    with _lock:
        if _client is None:
            from schwab_sync import TOKEN_FILE, _env
            from schwab import auth
            if not TOKEN_FILE.exists():
                return None
            _client = auth.client_from_token_file(str(TOKEN_FILE), _env("SCHWAB_APP_KEY"),
                                                  _env("SCHWAB_APP_SECRET"))
        return _client


def price(ticker: str):
    """Last price; after hours, the extended-session price if it traded later."""
    try:
        c = _get_client()
        if c is None:
            return None
        q = c.get_quote(ticker).json().get(ticker, {})
        reg, ext = q.get("quote") or {}, q.get("extended") or {}
        if ext.get("lastPrice") and ext.get("tradeTime", 0) > reg.get("tradeTime", 0):
            return float(ext["lastPrice"])
        return float(reg["lastPrice"]) if reg.get("lastPrice") else None
    except Exception:
        return None
