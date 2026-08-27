import json
import logging
import os
import threading
import time

import pandas as pd
import yfinance as yf

# Suppress noisy yfinance warnings ("No earnings dates found, symbol may be delisted")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
from dotenv import load_dotenv

from .constants import ROOT_DIR, SECTOR_ETFS, SECTOR_CACHE_FILE, FUNDAMENTAL_CACHE_FILE

# Load .env for Alpaca credentials (optional — scanner works without them)
load_dotenv(ROOT_DIR / ".env")

# ---------------------------------------------------------------------------
# Alpaca client — lazy-initialized, None if no keys configured
# ---------------------------------------------------------------------------
_alpaca_client = None
_alpaca_init_done = False
_alpaca_lock = threading.Lock()


def _get_alpaca_client():
    """Lazy-init Alpaca data client. Returns None if keys not set."""
    global _alpaca_client, _alpaca_init_done
    if _alpaca_init_done:
        return _alpaca_client
    with _alpaca_lock:
        if _alpaca_init_done:
            return _alpaca_client
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_API_SECRET", "").strip()
        if key and secret:
            try:
                from alpaca.data import StockHistoricalDataClient
                _alpaca_client = StockHistoricalDataClient(key, secret)
                print("  [DATA] Alpaca connected — real-time quotes enabled")
            except Exception as e:
                print(f"  [DATA] Alpaca init failed ({e}) — using yfinance only")
                _alpaca_client = None
        _alpaca_init_done = True
        return _alpaca_client


# ---------------------------------------------------------------------------
# Rate limiter — prevents yfinance throttling with concurrent threads
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_last_call = 0.0
_MIN_DELAY = 0.12  # 120ms between API calls = max ~8 req/sec (was 80ms/12rps — too aggressive for 500+ tickers)
_cooldown_until = 0.0  # global cooldown: all threads pause until this time

# API call counter — reset each scan, read at end for metrics
_api_call_count = 0
_api_call_lock = threading.Lock()


def reset_api_counter():
    """Reset the API call counter. Call at start of each scan."""
    global _api_call_count
    with _api_call_lock:
        _api_call_count = 0


def get_api_call_count() -> int:
    """Return total yfinance API calls since last reset."""
    return _api_call_count


def _rate_limit():
    """Rate limiter with global cooldown. Call before each yfinance API request."""
    global _last_call, _api_call_count
    # If a cooldown is active (rate limit hit), wait it out
    now = time.monotonic()
    if now < _cooldown_until:
        time.sleep(_cooldown_until - now)
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_call
        if elapsed < _MIN_DELAY:
            time.sleep(_MIN_DELAY - elapsed)
        _last_call = time.monotonic()
    with _api_call_lock:
        _api_call_count += 1


def _trigger_cooldown(seconds: float = 5.0):
    """Set a global cooldown — all threads pause for `seconds`."""
    global _cooldown_until
    _cooldown_until = time.monotonic() + seconds


_crumb_lock = threading.Lock()
_last_crumb_reset = 0.0


def _reset_yf_crumb():
    """Clear yfinance's cached crumb/cookie to force re-auth on 401 errors.
    Rate-limited to at most once per 30 seconds to avoid stampede."""
    global _last_crumb_reset
    with _crumb_lock:
        now = time.monotonic()
        if now - _last_crumb_reset < 30:
            return  # Already reset recently
        _last_crumb_reset = now
        _trigger_cooldown(5.0)  # pause all threads while crumb refreshes
        try:
            # yfinance 1.x: crumb lives on the YfData singleton
            from yfinance.data import YfData
            yd = YfData()
            if hasattr(yd, '_crumb'):
                yd._crumb = None
                yd._cookie = None
        except Exception:
            pass


def load_config():
    config_path = ROOT_DIR / "config.json"
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# OHLCV TTL cache — avoids re-fetching on repeat/rescan within same session
# ---------------------------------------------------------------------------
_ohlcv_cache = {}  # key: (ticker, period) -> {"data": DataFrame, "ts": monotonic}
_ohlcv_cache_lock = threading.Lock()
_OHLCV_TTL = 55 * 60  # 55 minutes — stale before next hourly scan


def clear_ohlcv_cache():
    """Clear the OHLCV cache (e.g. when forcing a fresh scan)."""
    global _ohlcv_cache
    with _ohlcv_cache_lock:
        _ohlcv_cache = {}


def fetch_intraday_data(ticker: str, period: str = "5d",
                        interval: str = "15m") -> pd.DataFrame:
    """Fetch intraday bars for same-bar conflict resolution.
    Only called when daily bars show ambiguity (both stop AND TP hit on same bar).
    Returns empty DataFrame on failure (caller falls back to daily heuristic)."""
    _rate_limit()
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
        return df
    except Exception:
        return pd.DataFrame()


def fetch_stock_data(ticker: str, period: str = "1y") -> pd.DataFrame:
    # Check TTL cache first
    cache_key = (ticker, period)
    with _ohlcv_cache_lock:
        cached = _ohlcv_cache.get(cache_key)
        if cached and (time.monotonic() - cached["ts"]) < _OHLCV_TTL:
            return cached["data"]

    for attempt in range(3):
        _rate_limit()
        try:
            df = yf.Ticker(ticker).history(period=period)
            with _ohlcv_cache_lock:
                _ohlcv_cache[cache_key] = {"data": df, "ts": time.monotonic()}
            return df
        except Exception as e:
            err = str(e)
            if "401" in err or "Crumb" in err or "Too Many" in err or "429" in err:
                _reset_yf_crumb()
                time.sleep(2 * (attempt + 1))  # backoff: 2s, 4s, 6s
                continue
            raise
    # Final attempt after all retries exhausted
    _rate_limit()
    df = yf.Ticker(ticker).history(period=period)
    with _ohlcv_cache_lock:
        _ohlcv_cache[cache_key] = {"data": df, "ts": time.monotonic()}
    return df


def _alpaca_quote(ticker: str) -> float | None:
    """Get real-time quote from Alpaca. Returns None on failure."""
    client = _get_alpaca_client()
    if client is None:
        return None
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quotes = client.get_stock_latest_quote(req)
        q = quotes.get(ticker)
        if q is None:
            return None
        # Use midpoint of bid/ask if both available, else ask, else bid
        bid, ask = q.bid_price, q.ask_price
        if bid and ask and bid > 0 and ask > 0:
            return float((bid + ask) / 2)
        if ask and ask > 0:
            return float(ask)
        if bid and bid > 0:
            return float(bid)
    except Exception:
        pass
    return None


def get_live_price(ticker: str, extended_hours: bool = False) -> float | None:
    """Get the most current price available.
    Market hours: tries Alpaca (real-time IEX) first, falls back to yfinance fast_info.
    Extended hours: uses yfinance t.info for post/pre-market prices (Alpaca free has no AH).
    Returns None if unavailable — caller should fall back to df['Close'].iloc[-1]."""
    # During market hours: Alpaca real-time > yfinance fast_info
    if not extended_hours:
        p = _alpaca_quote(ticker)
        if p is not None:
            return p
        # Fallback: yfinance fast_info
        try:
            _rate_limit()
            p = getattr(yf.Ticker(ticker).fast_info, "last_price", None)
            if p is not None and p > 0:
                return float(p)
        except Exception:
            pass
        return None

    # Extended hours: yfinance t.info (has postMarketPrice / preMarketPrice)
    for attempt in range(2):
        try:
            _rate_limit()
            info = yf.Ticker(ticker).info
            for key in ("postMarketPrice", "preMarketPrice"):
                p = info.get(key)
                if p is not None and p > 0:
                    return float(p)
            p = info.get("regularMarketPrice")
            if p is not None and p > 0:
                return float(p)
            break
        except Exception as e:
            if attempt == 0 and ("401" in str(e) or "Crumb" in str(e)):
                _reset_yf_crumb()
                continue
            break
    return None


def fetch_weekly_data(ticker: str) -> pd.DataFrame:
    df = fetch_stock_data(ticker, period="2y")
    if df.empty:
        return df
    weekly = df.resample("W").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()
    return weekly


# Reverse map: ETF ticker -> sector name (for ETFs that have no yfinance sector)
_ETF_SECTOR_MAP = {etf: sector for sector, etf in SECTOR_ETFS.items()}
# Non-rotation ETFs in the ticker universe (yfinance returns no sector for these)
_ETF_SECTOR_MAP.update({
    "DIA": "Industrials",         # Dow Jones Industrial Average ETF
    "KRE": "Financial Services",  # Regional Banking ETF
    "GLD": "Basic Materials",     # SPDR Gold Shares ETF
    "SLV": "Basic Materials",     # iShares Silver Trust ETF
    "GDX": "Basic Materials",     # VanEck Gold Miners ETF
    "PDBC": "Basic Materials",    # Invesco Diversified Commodity ETF
    "URA": "Basic Materials",     # Global X Uranium ETF
})

# In-memory sector cache — loaded once, written on new entries only
_sector_cache = None
_sector_cache_lock = threading.Lock()


def _get_sector_cache() -> dict:
    """Load sector cache once into memory. Thread-safe."""
    global _sector_cache
    if _sector_cache is None:
        with _sector_cache_lock:
            if _sector_cache is None:
                if SECTOR_CACHE_FILE.exists():
                    try:
                        _sector_cache = json.loads(SECTOR_CACHE_FILE.read_text(encoding="utf-8"))
                    except Exception:
                        _sector_cache = {}
                else:
                    _sector_cache = {}
    return _sector_cache


def _save_sector_entry(ticker: str, sector: str, industry: str):
    """Add entry to in-memory cache and persist to disk."""
    cache = _get_sector_cache()
    with _sector_cache_lock:
        cache[ticker] = {"sector": sector, "industry": industry}
        SECTOR_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fundamental cache — 24h expiry (fundamentals don't change intraday)
# ---------------------------------------------------------------------------
_fundamental_cache = None
_fundamental_cache_lock = threading.Lock()


def _get_fundamental_cache() -> dict:
    """Load fundamental cache once into memory. Thread-safe."""
    global _fundamental_cache
    if _fundamental_cache is None:
        with _fundamental_cache_lock:
            if _fundamental_cache is None:
                if FUNDAMENTAL_CACHE_FILE.exists():
                    try:
                        _fundamental_cache = json.loads(
                            FUNDAMENTAL_CACHE_FILE.read_text(encoding="utf-8")
                        )
                    except Exception:
                        _fundamental_cache = {}
                else:
                    _fundamental_cache = {}
    return _fundamental_cache


def _extract_fundamentals(info: dict) -> dict:
    """Extract fundamental fields from a yfinance .info dict."""
    return {
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "peg_ratio": info.get("pegRatio"),
        "profit_margins": info.get("profitMargins"),
        "operating_margins": info.get("operatingMargins"),
        "debt_to_equity": info.get("debtToEquity"),
        "return_on_equity": info.get("returnOnEquity"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "free_cashflow": info.get("freeCashflow"),
        "market_cap": info.get("marketCap"),
        # Market intelligence (same .info call, no extra API cost)
        "short_pct_float": info.get("shortPercentOfFloat"),
        "short_ratio": info.get("shortRatio"),
        "float_shares": info.get("floatShares"),
        "shares_outstanding": info.get("sharesOutstanding"),
        "target_mean_price": info.get("targetMeanPrice"),
        "analyst_count": info.get("numberOfAnalystOpinions"),
        "recommendation": info.get("recommendationKey"),
        "inst_pct": info.get("heldPercentInstitutions"),
        "insider_pct": info.get("heldPercentInsiders"),
        "beta": info.get("beta"),
    }


def _save_fundamental_entry(ticker: str, data: dict):
    """Save fundamental data to cache with timestamp."""
    cache = _get_fundamental_cache()
    with _fundamental_cache_lock:
        cache[ticker] = {"_timestamp": time.time(), **data}
        FUNDAMENTAL_CACHE_FILE.write_text(
            json.dumps(cache, indent=2), encoding="utf-8"
        )


def get_ticker_info(ticker: str) -> dict:
    # ETFs: use reverse map (yfinance never returns sector for ETFs)
    if ticker in _ETF_SECTOR_MAP:
        return {"sector": _ETF_SECTOR_MAP[ticker], "industry": "ETF"}

    # Check in-memory cache first (no disk I/O, no API call)
    cache = _get_sector_cache()
    if ticker in cache:
        return cache[ticker]

    # Try yfinance (rate-limited, with crumb retry)
    for attempt in range(2):
        try:
            _rate_limit()
            info = yf.Ticker(ticker).info
            sector = info.get("sector", "")
            industry = info.get("industry", "")
            if sector:
                _save_sector_entry(ticker, sector, industry)
                # Also cache fundamentals from the same API call (avoids double fetch)
                _save_fundamental_entry(ticker, _extract_fundamentals(info))
                return {"sector": sector, "industry": industry}
            break
        except Exception as e:
            if attempt == 0 and ("401" in str(e) or "Crumb" in str(e)):
                _reset_yf_crumb()
                continue
            break

    return {"sector": "Unknown", "industry": "Unknown"}


def get_fundamentals(ticker: str) -> dict:
    """Get expanded fundamental data with 24h caching.
    Returns dict with PE, margins, debt, growth, cash flow fields.
    Most calls are cache hits (populated by get_ticker_info)."""
    cache = _get_fundamental_cache()
    if ticker in cache:
        entry = cache[ticker]
        age = time.time() - entry.get("_timestamp", 0)
        if age < 86400:  # 24 hours
            return {k: v for k, v in entry.items() if k != "_timestamp"}

    # Not cached or stale — fetch from yfinance
    try:
        _rate_limit()
        info = yf.Ticker(ticker).info
        data = _extract_fundamentals(info)
        _save_fundamental_entry(ticker, data)
        return data
    except Exception:
        return {}


def get_market_intel(ticker: str) -> dict:
    """Get market intelligence data (short interest, float, analyst targets,
    institutional ownership, beta). Reads from fundamental cache — no API call."""
    fund = get_fundamentals(ticker)
    return {
        "short_pct_float": fund.get("short_pct_float"),
        "short_ratio": fund.get("short_ratio"),
        "float_shares": fund.get("float_shares"),
        "shares_outstanding": fund.get("shares_outstanding"),
        "target_mean_price": fund.get("target_mean_price"),
        "analyst_count": fund.get("analyst_count"),
        "recommendation": fund.get("recommendation"),
        "inst_pct": fund.get("inst_pct"),
        "insider_pct": fund.get("insider_pct"),
        "beta": fund.get("beta"),
    }


def get_fundamental_info(ticker: str) -> dict:
    """Get fundamental data for deep dive (P/E, PEG, market cap).
    Now reads from the shared fundamental cache."""
    fund = get_fundamentals(ticker)
    return {
        "trailing_pe": fund.get("trailing_pe"),
        "forward_pe": fund.get("forward_pe"),
        "peg_ratio": fund.get("peg_ratio"),
        "market_cap": fund.get("market_cap"),
    }
