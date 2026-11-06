"""A year of daily candles per ticker for the page's charts.

One batch download for every ticker on the page, kept in cache/candles.json
so a re-render within the hour doesn't fetch again. Split-adjusted, so a
split doesn't show as a cliff.
"""

import json
import time

from .constants import CACHE_DIR

CANDLE_FILE = CACHE_DIR / "candles.json"
_TTL = 55 * 60  # same as the scan's OHLCV cache


def _round(x: float) -> float:
    return round(float(x), 4 if x < 1 else 2)


def load_candles(tickers: list) -> dict:
    """ticker -> [[YYYY-MM-DD, open, high, low, close], ...], oldest first.
    Tickers the download misses are left out; their charts just don't draw."""
    try:
        cache = json.loads(CANDLE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    now = time.time()
    need = [t for t in tickers if now - cache.get(t, {}).get("ts", 0) > _TTL]
    if need:
        import yfinance as yf
        try:
            df = yf.download(need, period="1y", interval="1d", group_by="ticker",
                             auto_adjust=True, threads=True, progress=False)
        except Exception:
            df = None
        for t in need if df is not None and not df.empty else []:
            try:
                d = df[t].dropna()
            except KeyError:
                if len(need) > 1:
                    continue
                d = df.dropna()  # older yfinance: one ticker comes back flat
            bars = [[i.strftime("%Y-%m-%d"), _round(r.Open), _round(r.High), _round(r.Low), _round(r.Close)]
                    for i, r in d.iterrows()]
            if bars:
                cache[t] = {"ts": now, "bars": bars}
        try:
            CANDLE_FILE.write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            pass
    return {t: cache[t]["bars"] for t in tickers if t in cache}
