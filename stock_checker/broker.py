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


BATCH = 100   # tickers per request - ~5 requests for the whole book


def regular_prices(tickers: list) -> dict:
    """{ticker: regular-session last price} for many tickers at once.

    The regular session only: during the day that is the real-time price,
    after the close it is the official close. After-hours prints are left
    out on purpose - one thin trade at 6 PM should not hit a rung.
    Tickers Schwab can't price are simply missing; callers fall back.
    """
    out = {}
    try:
        c = _get_client()
        if c is None:
            return out
        for i in range(0, len(tickers), BATCH):
            chunk = tickers[i:i + BATCH]
            # Schwab writes share classes with a slash: BRK/B
            names = {t.replace("-", "/"): t for t in chunk}
            r = c.get_quotes(list(names))
            if r.status_code != 200:
                continue
            for sym, q in r.json().items():
                p = (q.get("regular") or {}).get("regularMarketLastPrice")                     or (q.get("quote") or {}).get("lastPrice")
                if p and sym in names:
                    out[names[sym]] = float(p)
    except Exception:
        pass
    return out
