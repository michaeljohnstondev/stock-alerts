"""Where a click on a price opens the chart - set by "chart_site" in config.json.

    "tradingview"  (default) only needs the ticker
    "webull"       Webull's quote page, which needs the listing exchange
                   (webull.com/quote/nasdaq-aapl, nyse-ko, nysearca-spy)

Exchanges are looked up once from Yahoo and cached in cache/exchanges.json.
A ticker whose exchange is unknown falls back to TradingView.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

from .constants import CACHE_DIR
from .data import load_config

EXCHANGE_FILE = CACHE_DIR / "exchanges.json"

SITES = {
    "tradingview": "https://www.tradingview.com/chart/?symbol={t}",
    "webull": "https://www.webull.com/quote/{ex}-{t}",
}

# Yahoo exchange code -> Webull's prefix
WEBULL_EXCHANGE = {
    "NMS": "nasdaq", "NGM": "nasdaq", "NCM": "nasdaq", "NAS": "nasdaq",
    "NYQ": "nyse", "NYS": "nyse",
    "PCX": "nysearca", "ARCA": "nysearca",
    "BTS": "bats",  # Cboe BZX - ARKK, IGV
}


def _load() -> dict:
    try:
        return json.loads(EXCHANGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _lookup(ticker: str):
    try:
        return ticker, yf.Ticker(ticker).fast_info["exchange"]
    except Exception:
        return ticker, None


def exchanges_for(tickers: list) -> dict:
    """Cached exchange codes, fetching any that are missing."""
    cache = _load()
    missing = [t for t in tickers if t not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for t, ex in pool.map(_lookup, missing):
                if ex:
                    cache[t] = ex
        EXCHANGE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    return cache


def chart_linker(tickers: list):
    """A function ticker -> chart URL, per the config setting."""
    site = (load_config() or {}).get("chart_site", "tradingview").lower()
    if site != "webull":
        return lambda t: SITES["tradingview"].format(t=t)
    ex = exchanges_for(tickers)

    def link(t: str) -> str:
        prefix = WEBULL_EXCHANGE.get(ex.get(t, ""))
        if not prefix:
            return SITES["tradingview"].format(t=t)
        # Webull writes share classes with a dash and lower case: brk-b
        return SITES["webull"].format(ex=prefix, t=t.lower().replace(".", "-"))
    return link
