import json
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

from .constants import (
    STATE_FILE, SECTOR_STATE_FILE, SECTOR_ETFS, EARNINGS_NOTE_DAYS,
    MARKET_CONTEXT,
)
from .data import fetch_stock_data, fetch_weekly_data, _rate_limit, _reset_yf_crumb
from .indicators import calculate_200ma, calculate_adx
from .adaptive import get_adaptive_regime_thresholds


# ---------------------------------------------------------------------------
# Relative Strength vs SPY
# ---------------------------------------------------------------------------

def calculate_relative_strength(df: pd.DataFrame, spy_df: pd.DataFrame) -> float:
    """Calculate stock's relative strength vs SPY over 50 days.
    Returns difference in % change (positive = outperforming SPY)."""
    if len(df) < 50 or len(spy_df) < 50:
        return None
    stock_roc = (df["Close"].iloc[-1] / df["Close"].iloc[-50] - 1) * 100
    spy_roc = (spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-50] - 1) * 100
    return round(stock_roc - spy_roc, 1)


# ---------------------------------------------------------------------------
# Regime state persistence (regime change detection)
# ---------------------------------------------------------------------------

def save_regime_state(regime: str, adx: float):
    """Save current regime to state file for change detection between scans."""
    state = {"regime": regime, "adx": round(adx, 1),
             "timestamp": datetime.now().isoformat()}
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def load_regime_state() -> dict:
    """Load previous regime state, or None if no prior state."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def check_regime_change(current_regime: str, current_adx: float) -> dict:
    """Compare current regime to previous scan. Save new state.
    Returns dict with 'changed' bool and transition details."""
    prev = load_regime_state()
    save_regime_state(current_regime, current_adx)
    if prev is None or prev.get("regime") == current_regime:
        return {"changed": False}
    return {
        "changed": True,
        "from_regime": prev["regime"],
        "to_regime": current_regime,
        "from_adx": prev.get("adx", 0),
        "to_adx": round(current_adx, 1),
        "prev_time": prev.get("timestamp", "unknown"),
    }


# ---------------------------------------------------------------------------
# Earnings calendar — batch fetch upcoming earnings for all tickers
# ---------------------------------------------------------------------------

_earnings_calendar_cache: dict | None = None


def _scrape_yahoo_earnings_day(day_str: str) -> list:
    """Scrape one day of Yahoo Finance earnings calendar.

    Returns list of ticker strings found on that day.
    Yahoo embeds earnings data as JSON in a <script> tag.
    """
    import re
    tickers = []
    try:
        url = f"https://finance.yahoo.com/calendar/earnings?day={day_str}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return tickers

        # Yahoo embeds earnings data in <script> tags as JSON.
        # Look for the script containing the screener result.
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        import json
        for s in scripts:
            if not s.startswith('{"status":'):
                continue
            try:
                outer = json.loads(s)
                body = json.loads(outer.get("body", "{}"))
                result = body.get("finance", {}).get("result", [{}])[0]
                docs = result.get("documents", [])
                if not docs:
                    continue
                doc = docs[0]
                rows = doc.get("rows", [])
                # Each row is a list: [ticker, company_name, event, date, ...]
                for row in rows:
                    if isinstance(row, list) and len(row) > 0 and isinstance(row[0], str):
                        sym = row[0].strip().upper()
                        if sym and len(sym) <= 5:
                            tickers.append(sym)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
    except Exception:
        pass
    return tickers


def fetch_earnings_calendar(days_ahead: int = 10, days_back: int = 7) -> dict:
    """Fetch Yahoo Finance earnings calendar for past N days + next N days.

    Returns {ticker: [date_str, ...]} for every company reporting.
    Each ticker maps to a sorted list of all earnings dates found.
    Results are cached for the duration of the scan (module-level).
    This is a safety net — the primary check is per-ticker via yfinance.
    """
    global _earnings_calendar_cache
    if _earnings_calendar_cache is not None:
        return _earnings_calendar_cache

    result = {}
    today = datetime.now().date()

    for offset in range(-days_back, days_ahead + 1):
        day = today + timedelta(days=offset)
        day_str = day.strftime("%Y-%m-%d")
        # Skip weekends
        if day.weekday() >= 5:
            continue
        tickers = _scrape_yahoo_earnings_day(day_str)
        for sym in tickers:
            if sym not in result:
                result[sym] = []
            if day_str not in result[sym]:
                result[sym].append(day_str)
        time.sleep(0.5)

    # Sort each ticker's dates chronologically
    for sym in result:
        result[sym].sort()

    _earnings_calendar_cache = result
    return result


def reset_earnings_calendar_cache():
    """Reset the earnings calendar cache (call at scan start)."""
    global _earnings_calendar_cache
    _earnings_calendar_cache = None


# ---------------------------------------------------------------------------
# Earnings proximity check
# ---------------------------------------------------------------------------


def check_earnings_proximity(ticker: str, prefetched_dates: set = None,
                             earnings_calendar: dict = None) -> dict:
    """Check if earnings are within the danger zone (upcoming or recent).
    Only called for tickers with qualifying signals (not all 492).
    If prefetched_dates is provided (from generate_signals), skips the API call.
    Otherwise uses t.earnings_dates first, falls back to t.calendar if rate-limited.
    Returns {"near_earnings": bool, "days_until": int or None, "date": str or None,
             "days_since": int or None, "last_date": str or None}"""
    no_data = {"near_earnings": False, "days_until": None, "date": None,
               "days_since": None, "last_date": None}
    try:
        today = pd.Timestamp.now().normalize()
        next_date = None
        last_date = None  # most recent past earnings
        all_dates = []

        if prefetched_dates:
            # Reuse dates already fetched by generate_signals — no API call needed
            all_dates = sorted(prefetched_dates)
            future = [d for d in all_dates if d >= today]
            past = [d for d in all_dates if d < today]
            if future:
                next_date = future[0]
            if past:
                last_date = past[-1]

        if next_date is None and not prefetched_dates:
            # No prefetched dates at all — try per-ticker API
            _rate_limit()
            t = yf.Ticker(ticker)
            try:
                _rate_limit()
                dates = t.earnings_dates
                if dates is not None and not dates.empty:
                    idx = dates.index.tz_localize(None) if dates.index.tz is not None else dates.index
                    future = idx[idx >= today]
                    past = idx[idx < today]
                    if not future.empty:
                        next_date = future[0]
                    if not past.empty and last_date is None:
                        last_date = past[-1]
            except Exception as e:
                if "401" in str(e) or "Crumb" in str(e):
                    _reset_yf_crumb()

        # Fallback: t.calendar (works when t.earnings_dates has no future dates)
        # This catches tickers like D where earnings_dates is historical-only
        # but calendar has the upcoming date.
        if next_date is None:
            try:
                _rate_limit()
                t = yf.Ticker(ticker)
                cal = t.calendar
                if cal and "Earnings Date" in cal:
                    earn_dates = cal["Earnings Date"]
                    if not isinstance(earn_dates, list):
                        earn_dates = [earn_dates]
                    for ed in earn_dates:
                        ed_ts = pd.Timestamp(ed).normalize()
                        if ed_ts >= today:
                            next_date = ed_ts
                            break
            except Exception:
                pass

        # --- CALENDAR CROSS-REFERENCE ---
        # If yfinance missed this ticker but the earnings calendar has it,
        # inject the date.  This catches cases where t.earnings_dates is
        # stale or empty (e.g. ticker "D" reporting today).
        # Calendar format: {ticker: [date_str, ...]} (sorted list of dates)
        if earnings_calendar and ticker in earnings_calendar:
            cal_dates = earnings_calendar[ticker]
            # Handle both old format (single string) and new format (list)
            if isinstance(cal_dates, str):
                cal_dates = [cal_dates]
            for ds in cal_dates:
                cal_date = pd.Timestamp(ds).normalize()
                if cal_date >= today and next_date is None:
                    next_date = cal_date
                elif cal_date < today and (last_date is None or cal_date > last_date):
                    last_date = cal_date  # most recent past date

        result = dict(no_data)

        if next_date is not None:
            delta = (next_date - today).days
            result["near_earnings"] = delta <= EARNINGS_NOTE_DAYS
            result["days_until"] = delta
            result["date"] = next_date.strftime("%Y-%m-%d")

        if last_date is not None:
            days_since = (today - last_date).days
            if days_since <= 14:  # only report if within 2 weeks
                result["days_since"] = days_since
                result["last_date"] = last_date.strftime("%Y-%m-%d")

        return result
    except Exception:
        return no_data


# ---------------------------------------------------------------------------
# Sector rotation analysis
# ---------------------------------------------------------------------------

def calculate_sector_rotation(spy_df: pd.DataFrame) -> dict:
    """Calculate sector momentum vs SPY over 1d, 10d, 30d, 50d.
    Returns dict keyed by sector name with rank, returns, and status."""
    if spy_df.empty or len(spy_df) < 50:
        return {}

    spy_ret_1 = (spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-2] - 1) * 100
    spy_ret_10 = (spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-10] - 1) * 100
    spy_ret_30 = (spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-30] - 1) * 100
    spy_ret_50 = (spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-50] - 1) * 100

    sectors = {}
    for sector, etf in SECTOR_ETFS.items():
        try:
            df = fetch_stock_data(etf, period="6mo")
            if len(df) < 50:
                continue
            ret_1 = (df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100 - spy_ret_1
            ret_10 = (df["Close"].iloc[-1] / df["Close"].iloc[-10] - 1) * 100 - spy_ret_10
            ret_30 = (df["Close"].iloc[-1] / df["Close"].iloc[-30] - 1) * 100 - spy_ret_30
            ret_50 = (df["Close"].iloc[-1] / df["Close"].iloc[-50] - 1) * 100 - spy_ret_50
            composite = 0.5 * ret_10 + 0.3 * ret_30 + 0.2 * ret_50
            sectors[sector] = {
                "etf": etf,
                "close": round(float(df["Close"].iloc[-1]), 2),
                "ret_1d": round(ret_1, 1),
                "ret_10d": round(ret_10, 1),
                "ret_30d": round(ret_30, 1),
                "ret_50d": round(ret_50, 1),
                "composite": round(composite, 1),
            }
        except Exception:
            continue

    # Rank by composite
    ranked = sorted(sectors.items(), key=lambda x: x[1]["composite"], reverse=True)
    for i, (sector, data) in enumerate(ranked, 1):
        data["rank"] = i
        if i <= 3:
            data["status"] = "leading"
        elif i >= len(ranked) - 2:
            data["status"] = "lagging"
        else:
            data["status"] = "neutral"

    return sectors


def save_sector_state(rotation: dict, spy_close: float = 0):
    """Save current sector rotation to state file for change detection between scans."""
    state = {
        "timestamp": datetime.now().isoformat(),
        "spy_close": spy_close,
        "sectors": {
            sector: {
                "rank": data["rank"],
                "composite": data["composite"],
                "status": data["status"],
                "close": data.get("close", 0),
            }
            for sector, data in rotation.items()
        },
    }
    SECTOR_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def load_sector_state() -> dict:
    """Load previous sector rotation state, or None if no prior state."""
    if SECTOR_STATE_FILE.exists():
        try:
            return json.loads(SECTOR_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def check_sector_changes(current_rotation: dict, spy_close: float = 0) -> dict:
    """Compare current sector rotation to previous scan. Save new state.
    Returns dict with per-sector changes and key transitions."""
    prev = load_sector_state()
    save_sector_state(current_rotation, spy_close=spy_close)

    if prev is None or not prev.get("sectors"):
        return {"has_prev": False, "changes": {}, "transitions": []}

    prev_sectors = prev["sectors"]
    prev_spy = prev.get("spy_close", 0)
    # SPY return since last scan (for relative sector change)
    spy_ret = ((spy_close / prev_spy) - 1) * 100 if prev_spy > 0 and spy_close > 0 else 0
    changes = {}
    transitions = []

    for sector, data in current_rotation.items():
        if sector not in prev_sectors:
            continue
        p = prev_sectors[sector]
        rank_delta = p["rank"] - data["rank"]  # positive = improved (moved up)
        comp_delta = data["composite"] - p["composite"]  # positive = accelerating
        status_changed = p["status"] != data["status"]

        # ETF price % change since last scan (vs SPY, consistent with other columns)
        prev_close = p.get("close", 0)
        cur_close = data.get("close", 0)
        raw_chg = ((cur_close / prev_close) - 1) * 100 if prev_close > 0 else 0
        price_chg = raw_chg - spy_ret  # excess return vs SPY

        changes[sector] = {
            "rank_delta": rank_delta,
            "comp_delta": round(comp_delta, 1),
            "price_chg": round(price_chg, 1),
            "prev_rank": p["rank"],
            "prev_composite": p["composite"],
            "prev_status": p["status"],
            "status_changed": status_changed,
        }

        if status_changed:
            transitions.append({
                "sector": sector,
                "from": p["status"],
                "to": data["status"],
                "rank_delta": rank_delta,
            })

    return {
        "has_prev": True,
        "prev_time": prev.get("timestamp", "unknown"),
        "changes": changes,
        "transitions": transitions,
    }


# ---------------------------------------------------------------------------
# Market trend (Law 5)
# ---------------------------------------------------------------------------

def get_market_trend(config: dict, prefetched: dict = None) -> dict:
    """Get market trend for benchmarks. Accepts prefetched data to avoid duplicate API calls."""
    if prefetched is None:
        prefetched = {}
    benchmarks = config.get("market_alignment", {}).get("benchmarks", ["SPY", "QQQ"])
    trends = {}
    for sym in benchmarks:
        try:
            df = prefetched.get(sym)
            if df is None or df.empty:
                df = fetch_stock_data(sym, period="1y")
            if len(df) < 200:
                trends[sym] = "neutral"
                continue
            ma200 = calculate_200ma(df)
            price = df["Close"].iloc[-1]
            ma_val = ma200.iloc[-1]
            if pd.isna(ma_val):
                trends[sym] = "neutral"
            elif price > ma_val:
                trends[sym] = "bullish"
            else:
                trends[sym] = "bearish"
        except Exception:
            trends[sym] = "neutral"
    return trends


def market_agrees(market_trends: dict, direction: str) -> bool:
    for trend in market_trends.values():
        if trend == "neutral":
            continue
        if trend != direction:
            return False
    return True


# Sector classification for rotation divergence detection
OFFENSIVE_SECTORS = {"Technology", "Financial Services", "Consumer Cyclical", "Communication Services"}
DEFENSIVE_SECTORS = {"Utilities", "Consumer Defensive", "Healthcare", "Energy", "Real Estate"}


def detect_rotation_divergence(market_trends: dict, sector_rotation: dict) -> dict:
    """Detect when sector rotation contradicts the market trend signal.

    E.g., SPY/QQQ 'bullish' (price > 200 MA) but defensive sectors leading
    suggests distribution phase — the 200 MA is lagging the actual rotation.

    Reports momentum: accelerating, fading, or unwinding based on 10d vs 30d.
    """
    result = {"divergent": False, "reason": "", "momentum": "",
              "defensive_leading": 0, "offensive_leading": 0}

    if not market_trends or not sector_rotation:
        return result

    # Determine overall market direction from benchmarks
    directions = [t for t in market_trends.values() if t != "neutral"]
    if not directions:
        return result
    mkt_direction = directions[0]  # SPY drives

    # Count offensive vs defensive in top-3 ranked sectors (by composite)
    top3 = [s for s, d in sector_rotation.items() if d.get("rank", 99) <= 3]
    defensive_leading = sum(1 for s in top3 if s in DEFENSIVE_SECTORS)
    offensive_leading = sum(1 for s in top3 if s in OFFENSIVE_SECTORS)
    result["defensive_leading"] = defensive_leading
    result["offensive_leading"] = offensive_leading

    # Avg 10d relative performance: defensive vs offensive sectors
    def_10d = [d["ret_10d"] for s, d in sector_rotation.items()
               if s in DEFENSIVE_SECTORS and "ret_10d" in d]
    off_10d = [d["ret_10d"] for s, d in sector_rotation.items()
               if s in OFFENSIVE_SECTORS and "ret_10d" in d]
    avg_def_10d = sum(def_10d) / len(def_10d) if def_10d else 0
    avg_off_10d = sum(off_10d) / len(off_10d) if off_10d else 0

    if mkt_direction == "bullish" and defensive_leading >= 2:
        result["divergent"] = True
        # Is the rotation still active or fading?
        if avg_def_10d > avg_off_10d + 1.0:
            result["momentum"] = "accelerating"
            result["reason"] = "Defensive rotation accelerating — defensive sectors still outpacing growth on 10d"
        elif avg_off_10d > avg_def_10d + 1.0:
            result["momentum"] = "unwinding"
            result["reason"] = "Defensive rotation unwinding — growth reclaiming 10d leadership"
        else:
            result["momentum"] = "fading"
            result["reason"] = "Defensive rotation fading — 30d defensive lead but 10d is flattening"
    elif mkt_direction == "bearish" and offensive_leading >= 2:
        result["divergent"] = True
        if avg_off_10d > avg_def_10d + 1.0:
            result["momentum"] = "accelerating"
            result["reason"] = "Offensive rotation accelerating — growth sectors outpacing defensives on 10d"
        elif avg_def_10d > avg_off_10d + 1.0:
            result["momentum"] = "unwinding"
            result["reason"] = "Offensive rotation unwinding — defensives reclaiming 10d leadership"
        else:
            result["momentum"] = "fading"
            result["reason"] = "Offensive rotation fading — 30d growth lead but 10d is flattening"

    return result


# ---------------------------------------------------------------------------
# Market regime detection
# ---------------------------------------------------------------------------

def detect_market_regime(config: dict) -> dict:
    """Detect market regime using SPY ADX with adaptive thresholds.
    Returns dict with regime label, ADX value, thresholds, and SPY DataFrame.
    Thresholds are learned from pick performance at different ADX levels.
    Defaults: CHOP < 18, RANGE 18-25, TREND > 25.
    """
    result = {"regime": "unknown", "adx": 0.0, "spy_df": pd.DataFrame(),
              "chop_threshold": 18.0, "trend_threshold": 25.0}

    # Step 1: Fetch SPY data (most important — used by RS, sector rotation, etc.)
    spy_df = None
    for attempt in range(3):
        try:
            spy_df = fetch_stock_data("SPY", period="1y")
            result["spy_df"] = spy_df
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))  # 1s, 2s backoff
            else:
                print(f"  WARNING: Failed to fetch SPY data after 3 attempts: {e}")
                return result

    if len(spy_df) < 200:
        print(f"  WARNING: SPY data too short ({len(spy_df)} bars)")
        return result

    # Step 2: Calculate ADX
    try:
        adx = calculate_adx(spy_df)
        adx_val = adx.iloc[-1]
        if pd.isna(adx_val):
            print("  WARNING: ADX calculation returned NaN")
            return result
        result["adx"] = float(adx_val)
    except Exception as e:
        print(f"  WARNING: ADX calculation failed: {e}")
        return result

    # Step 3: Adaptive thresholds (non-critical — fall back to defaults)
    try:
        thresholds = get_adaptive_regime_thresholds()
        result["chop_threshold"] = thresholds["chop_threshold"]
        result["trend_threshold"] = thresholds["trend_threshold"]
    except Exception as e:
        print(f"  WARNING: Adaptive thresholds failed (using defaults): {e}")

    chop_th = result["chop_threshold"]
    trend_th = result["trend_threshold"]

    if adx_val < chop_th:
        result["regime"] = "chop"
    elif adx_val < trend_th:
        result["regime"] = "range"
    else:
        result["regime"] = "trend"

    return result


# ---------------------------------------------------------------------------
# Higher timeframe (Law 23)
# ---------------------------------------------------------------------------

def get_higher_timeframe_trend(ticker: str, daily_df: pd.DataFrame = None) -> str:
    """Get weekly trend. Uses pre-fetched daily data (resampled to weekly) to avoid extra API call."""
    try:
        if daily_df is not None and not daily_df.empty:
            weekly = daily_df.resample("W").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
        else:
            weekly = fetch_weekly_data(ticker)
        if len(weekly) < 50:
            return "neutral"
        ma50w = weekly["Close"].rolling(window=50).mean()
        price = weekly["Close"].iloc[-1]
        ma_val = ma50w.iloc[-1]
        if pd.isna(ma_val):
            return "neutral"
        return "bullish" if price > ma_val else "bearish"
    except Exception:
        return "neutral"


# ---------------------------------------------------------------------------
# Market context — broader macro picture (rates, gold, crypto)
# ---------------------------------------------------------------------------

def get_market_context() -> dict:
    """Fetch trend for macro context ETFs (TLT, GLD, BTC-USD).
    Returns dict keyed by symbol with trend, % above 200 MA, and label.
    Informational only — not used for scoring."""
    context = {}
    for sym, label in MARKET_CONTEXT.items():
        try:
            df = fetch_stock_data(sym, period="1y")
            if len(df) < 200:
                context[sym] = {"label": label, "trend": "neutral", "pct_above_200": 0}
                continue
            ma200 = calculate_200ma(df)
            price = float(df["Close"].iloc[-1])
            ma_val = float(ma200.iloc[-1])
            if pd.isna(ma_val) or ma_val == 0:
                context[sym] = {"label": label, "trend": "neutral", "pct_above_200": 0}
                continue
            pct = (price - ma_val) / ma_val * 100
            trend = "bullish" if price > ma_val else "bearish"
            context[sym] = {"label": label, "trend": trend, "pct_above_200": round(pct, 1)}
        except Exception:
            context[sym] = {"label": label, "trend": "neutral", "pct_above_200": 0}
    return context
