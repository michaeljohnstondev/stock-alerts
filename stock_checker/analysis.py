import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from .constants import (
    SIGNAL_WEIGHTS, MAX_INDICATOR_SCORE, MAX_TREND_SCORE,
    MAX_VOLUME_SCORE, MAX_CONVICTION_SCORE,
    TIER_A_MIN, TIER_B_MIN,
    REGIME_CHOP_CAP, MKT_DISAGREE_CAP, CHOP_VOL_MIN, CHOP_TREND_VOL_MIN, CHOP_TREND_PLAY_DISCOUNT,
    EARNINGS_SKIP_DAYS, EARNINGS_WARN_DAYS, EARNINGS_NOTE_DAYS,
    EV_TIER_C_MAX, EV_TIER_A_MIN,
    TREND_DISCOUNT_COUNTER,
    RANGE_PLAY_SIGNALS, CHOP_RANGE_BOOST,
    CONFLICTING_SIGNAL_MAX_PENALTY,
    OVERBOUGHT_EXTENDED_PENALTY, OVERBOUGHT_ONLY_PENALTY, EXTENDED_ONLY_PENALTY,
    DIVERGENCE_MKT_CAP,
    MIN_SIGNALS, MIN_WEIGHT_3SIG,
)
from .data import fetch_stock_data, get_ticker_info, get_live_price, get_fundamentals, get_market_intel
from .indicators import (
    calculate_atr, calculate_200ma, calculate_ema, calculate_rsi,
    calculate_volume_ratio, calculate_fibonacci_levels,
    find_swing_low, find_swing_high,
    find_nearest_support, find_nearest_resistance,
)
from .market import (
    calculate_relative_strength, check_earnings_proximity,
    get_higher_timeframe_trend, market_agrees,
    OFFENSIVE_SECTORS, DEFENSIVE_SECTORS,
)
from .signals import generate_signals
from .scoring import (
    score_volume, check_trend_strength, calculate_score,
    calculate_ev, get_tier, get_risk_tier, _get_setup_context,
    score_value, compute_intel_adjustment,
)
from .preferences import compute_preference_boost
from .adaptive import get_play_type_win_rates

# Module-level cache for play type win rates (refreshed once per import / scan)
_play_type_wr_cache = None


def _get_play_type_wr_cached() -> dict:
    """Lazy-load play type win rates once per scan."""
    global _play_type_wr_cache
    if _play_type_wr_cache is None:
        _play_type_wr_cache = get_play_type_win_rates()
    return _play_type_wr_cache


def reset_play_type_wr_cache():
    """Call at start of each scan to refresh cached win rates."""
    global _play_type_wr_cache
    _play_type_wr_cache = None


def _passes_confluence(signals: list) -> bool:
    """Check if a signal group has enough confluence to qualify.
    4+ signals always pass. 3 signals pass if combined weight >= 2.5
    (prevents blocking elite setups while keeping the noise floor)."""
    if len(signals) >= MIN_SIGNALS:
        return True
    if len(signals) == 3:
        total_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in signals)
        return total_wt >= MIN_WEIGHT_3SIG
    return False


# ---------------------------------------------------------------------------
# Technical fingerprint for missed mover analysis
# ---------------------------------------------------------------------------

def _compute_mover_fingerprint(df: pd.DataFrame, signals: list,
                                trend_200: str) -> dict:
    """Compute technical fingerprint for a missed mover.
    Captures conditions beyond what signals cover so pattern gap
    analysis can identify recurring patterns in stocks that moved
    big but weren't flagged."""
    price = float(df["Close"].iloc[-1])
    fp = {}

    # Which signals fired (names only)
    fp["signals_fired"] = [s["indicator"] for s in signals]
    fp["trend_200"] = trend_200

    # --- OBV divergence (20d) ---
    lookback = df.iloc[-20:]
    obv = [0.0]
    for i in range(1, len(lookback)):
        if lookback["Close"].iloc[i] > lookback["Close"].iloc[i - 1]:
            obv.append(obv[-1] + lookback["Volume"].iloc[i])
        elif lookback["Close"].iloc[i] < lookback["Close"].iloc[i - 1]:
            obv.append(obv[-1] - lookback["Volume"].iloc[i])
        else:
            obv.append(obv[-1])
    obv_slope = obv[-1] - obv[0]
    price_chg = float(
        (lookback["Close"].iloc[-1] - lookback["Close"].iloc[0])
        / lookback["Close"].iloc[0]
    )
    fp["obv_diverging_up"] = bool(obv_slope > 0 and price_chg < 0.02)
    fp["obv_diverging_down"] = bool(obv_slope < 0 and price_chg > -0.02)

    # --- Up/down volume ratio (20d) ---
    up_vol = lookback.loc[
        lookback["Close"] > lookback["Close"].shift(1), "Volume"
    ].sum()
    dn_vol = lookback.loc[
        lookback["Close"] < lookback["Close"].shift(1), "Volume"
    ].sum()
    ud_ratio = float(up_vol / dn_vol) if dn_vol > 0 else 2.0
    fp["up_down_vol_ratio"] = round(ud_ratio, 2)
    fp["accumulation_vol"] = bool(ud_ratio > 1.3)
    fp["distribution_vol"] = bool(ud_ratio < 0.77)

    # --- Consolidation (20d range) ---
    range_pct = float(
        (lookback["High"].max() - lookback["Low"].min()) / lookback["Low"].min()
    )
    fp["consolidation_pct"] = round(range_pct * 100, 1)
    fp["tight_base"] = bool(range_pct < 0.10)

    # --- Trend structure: higher lows / lower highs ---
    if len(df) >= 20:
        fp["higher_lows"] = bool(
            float(df["Low"].iloc[-10:].min()) > float(df["Low"].iloc[-20:-10].min())
        )
        fp["lower_highs"] = bool(
            float(df["High"].iloc[-10:].max()) < float(df["High"].iloc[-20:-10].max())
        )
    else:
        fp["higher_lows"] = False
        fp["lower_highs"] = False

    # --- Volume expanding (3d vs 10d) ---
    if len(df) >= 13:
        vol_3d = float(df["Volume"].iloc[-3:].mean())
        vol_10d = float(df["Volume"].iloc[-13:-3].mean())
        fp["vol_expanding"] = bool(vol_10d > 0 and vol_3d > vol_10d * 1.15)
        fp["vol_ratio_3d"] = round(vol_3d / vol_10d, 2) if vol_10d > 0 else 1.0
    else:
        fp["vol_expanding"] = False
        fp["vol_ratio_3d"] = 1.0

    # --- Price vs key MAs ---
    if len(df) >= 200:
        ma200_val = float(df["Close"].rolling(200).mean().iloc[-1])
        fp["above_200ma"] = bool(not pd.isna(ma200_val) and price > ma200_val)
    else:
        fp["above_200ma"] = None
    if len(df) >= 50:
        ma50_val = float(df["Close"].rolling(50).mean().iloc[-1])
        fp["above_50ma"] = bool(not pd.isna(ma50_val) and price > ma50_val)
    else:
        fp["above_50ma"] = None

    # --- Near support / resistance ---
    support = find_nearest_support(df, price)
    resistance = find_nearest_resistance(df, price)
    fp["near_support"] = bool(support > 0 and abs(price - support) / support < 0.02)
    fp["near_resistance"] = bool(
        resistance > 0 and abs(price - resistance) / resistance < 0.02
    )

    # --- Recent gap (2%+ in last 5 days) ---
    fp["gap_recent"] = False
    max_gap_pct = 0.0
    for i in range(-5, -1):
        if abs(i) < len(df):
            gap = abs(
                float(df["Open"].iloc[i]) - float(df["Close"].iloc[i - 1])
            ) / float(df["Close"].iloc[i - 1])
            if gap > max_gap_pct:
                max_gap_pct = gap
            if gap >= 0.02:
                fp["gap_recent"] = True
    fp["max_gap_pct"] = round(max_gap_pct * 100, 1)

    # --- Likely catalyst (gap + extreme volume = news/earnings event) ---
    # A 2%+ gap combined with 2.5x+ volume expansion almost always means
    # earnings, FDA, M&A, or other binary event — not a technical pattern miss.
    fp["likely_catalyst"] = bool(
        fp["gap_recent"]
        and fp.get("vol_ratio_3d", 1.0) >= 2.5
    )

    # --- RSI zone (captures "almost oversold" that RSI signal misses) ---
    rsi = calculate_rsi(df, 14)
    rsi_val = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
    fp["rsi"] = round(rsi_val, 1)
    fp["rsi_near_oversold"] = bool(30 < rsi_val <= 40)
    fp["rsi_near_overbought"] = bool(60 <= rsi_val < 70)

    return fp


# ---------------------------------------------------------------------------
# Trade plan generator
# ---------------------------------------------------------------------------

def generate_trade_plan(df: pd.DataFrame, direction: str, score: float = 0,
                        trade_type: str = "", stop_mult: float = 1.5,
                        live_price: float = None) -> dict:
    """Generate entry, stop loss, take profit, key MAs, fibs, and risk tier.
    stop_mult: ATR multiplier for stop distance (default 1.5, adaptive range 1.0-2.5).
    live_price: current price when available (after-hours/pre-market)."""
    price = live_price if live_price is not None else df["Close"].iloc[-1]
    atr = calculate_atr(df)
    atr_val = atr.iloc[-1]
    if pd.isna(atr_val) or atr_val == 0:
        atr_val = price * 0.02

    # Key Moving Averages
    ma200 = calculate_200ma(df)
    ma200_val = ma200.iloc[-1] if not pd.isna(ma200.iloc[-1]) else None
    ma200_dist_pct = ((price - ma200_val) / ma200_val * 100) if ma200_val else None

    ma50_series = df["Close"].rolling(window=50).mean()
    ma50_val = ma50_series.iloc[-1] if len(df) >= 50 and not pd.isna(ma50_series.iloc[-1]) else None

    ma100_series = df["Close"].rolling(window=100).mean()
    ma100_val = ma100_series.iloc[-1] if len(df) >= 100 and not pd.isna(ma100_series.iloc[-1]) else None

    ma150_series = df["Close"].rolling(window=150).mean()
    ma150_val = ma150_series.iloc[-1] if len(df) >= 150 and not pd.isna(ma150_series.iloc[-1]) else None

    # EMA 21
    ema21_val = calculate_ema(df, 21).iloc[-1]

    # Support / resistance
    support = find_nearest_support(df, price)
    resistance = find_nearest_resistance(df, price)

    # Fibonacci levels
    fibs = calculate_fibonacci_levels(df)

    # Risk tier
    risk_tier = get_risk_tier(score, trade_type)

    # Build notes — check proximity to key MAs
    notes = []
    ma_levels = [
        ("50 MA", ma50_val),
        ("100 MA", ma100_val),
        ("150 MA", ma150_val),
        ("200 MA", ma200_val),
    ]
    for ma_name, ma_val in ma_levels:
        if ma_val:
            dist_pct = (price - ma_val) / ma_val * 100
            if 0 < abs(dist_pct) < 3:
                side = "above" if dist_pct > 0 else "below"
                notes.append(f"NEAR {ma_name}: {abs(dist_pct):.1f}% {side} (${ma_val:.2f})")

    if ma200_val and direction == "bullish" and ma200_dist_pct is not None:
        if ma200_dist_pct < 0:
            notes.append("BELOW 200 MA — day trade only")

    if ma200_val and direction == "bearish" and ma200_dist_pct is not None:
        if -3 < ma200_dist_pct < 0:
            notes.append(f"Only {abs(ma200_dist_pct):.1f}% below 200 MA — may reclaim")

    # Check if price is near a fib level (potential support/resistance)
    fib_labels = [
        ("23.6%", fibs["fib_236"]), ("38.2%", fibs["fib_382"]),
        ("50%", fibs["fib_500"]), ("61.8%", fibs["fib_618"]),
    ]
    for fib_name, fib_val in fib_labels:
        if fib_val > 0:
            dist_pct = abs(price - fib_val) / fib_val * 100
            if dist_pct < 1.5:
                notes.append(f"AT FIB {fib_name}: ${fib_val:.2f}")

    if direction == "bullish":
        swing_low = find_swing_low(df, 15)
        atr_stop = price - (atr_val * stop_mult)
        stop = max(atr_stop, swing_low - atr_val * 0.1, support - atr_val * 0.1)
        stop = min(stop, price * 0.995)
        risk = price - stop
        if risk <= 0:
            risk = atr_val
        tp1 = price + risk
        tp2 = price + risk * 2
        tp3 = price + risk * 3
    else:
        swing_high = find_swing_high(df, 15)
        atr_stop = price + (atr_val * stop_mult)
        stop = min(atr_stop, swing_high + atr_val * 0.1, resistance + atr_val * 0.1)
        stop = max(stop, price * 1.005)
        risk = stop - price
        if risk <= 0:
            risk = atr_val
        tp1 = price - risk
        tp2 = price - risk * 2
        tp3 = price - risk * 3

    # Structural TP1 cap — if resistance is between entry and TP1 (longs)
    # or support is between entry and TP1 (shorts), cap TP1 at the structure
    # level minus a small buffer, provided it still gives >= 0.3R profit.
    tp1_structural = False
    if direction == "bullish" and resistance > 0:
        struct_tp = resistance - atr_val * 0.1  # small buffer below resistance
        if price < struct_tp < tp1 and (struct_tp - price) >= risk * 0.3:
            notes.append(f"TP1 capped at structure ${struct_tp:.2f} (resistance ${resistance:.2f})")
            tp1 = struct_tp
            tp1_structural = True
    elif direction == "bearish" and support > 0:
        struct_tp = support + atr_val * 0.1  # small buffer above support
        if tp1 < struct_tp < price and (price - struct_tp) >= risk * 0.3:
            notes.append(f"TP1 capped at structure ${struct_tp:.2f} (support ${support:.2f})")
            tp1 = struct_tp
            tp1_structural = True

    return {
        "entry": round(price, 2),
        "stop": round(stop, 2),
        "risk": round(risk, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "tp3": round(tp3, 2),
        "tp1_structural": tp1_structural,
        "ma50": round(ma50_val, 2) if ma50_val else None,
        "ma100": round(ma100_val, 2) if ma100_val else None,
        "ma150": round(ma150_val, 2) if ma150_val else None,
        "ma200": round(ma200_val, 2) if ma200_val else None,
        "ma200_dist": round(ma200_dist_pct, 1) if ma200_dist_pct is not None else None,
        "ema21": round(ema21_val, 2),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "fibs": fibs,
        "risk_tier": risk_tier,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze_ticker(ticker: str, config: dict, market_trends: dict,
                   regime: str = "unknown", spy_df: pd.DataFrame = None,
                   sector_rotation: dict = None, adaptive_weights: dict = None,
                   pillar_mults: dict = None, adaptive_wr: dict = None,
                   stop_mult: float = 1.5, user_prefs: dict = None,
                   rotation_divergence: dict = None,
                   distrib_threshold: float = 0.77,
                   earnings_calendar: dict = None) -> tuple:
    """Analyze a single ticker. Returns (results, developing, move_info, dip_watch, approaching) tuple.
    results: list of scored trade setups
    developing: list of VCP-forming watchlist items
    move_info: dict if ticker had a big recent move (for missed mover detection), else None
    approaching: list of approaching/developing setup dicts"""
    results = []
    developing = []
    dip_watch = []
    approaching_items = []
    move_info = None
    leaderboard_entry = None
    try:
        df = fetch_stock_data(ticker, period="1y")
        if df.empty or len(df) < 200:
            return [], [], None, [], [], None

        current_price = df["Close"].iloc[-1]

        # Check if daily data is stale (pre-market / after hours)
        # Same-day after 4 PM ET counts as stale — close price != current price
        last_ts = df.index[-1]
        last_date = last_ts.tz_localize(None).date() if last_ts.tz else last_ts.date()
        now_et = datetime.now(ZoneInfo("America/New_York"))
        market_closed_today = (now_et.hour >= 16) or (now_et.weekday() >= 5)
        data_is_stale = (last_date < pd.Timestamp.now().date()) or (last_date == pd.Timestamp.now().date() and market_closed_today)

        # Detect big recent moves (for missed mover report)
        close = df["Close"]
        if len(close) >= 4:
            move_1d = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2])
            move_3d = float((close.iloc[-1] - close.iloc[-4]) / close.iloc[-4])
            if abs(move_1d) >= 0.03 or abs(move_3d) >= 0.05:
                move_info = {
                    "ticker": ticker,
                    "price": round(float(current_price), 2),
                    "move_1d": round(move_1d * 100, 1),
                    "move_3d": round(move_3d * 100, 1),
                    "signal_count": 0,
                    "bull_signals": 0,
                    "bear_signals": 0,
                }

        signals, trend_200, vcp, recent_earnings, earnings_dates, approaching_raw, sig_metrics, eps_data, rev_data = generate_signals(df, config, ticker=ticker, distrib_threshold=distrib_threshold)

        # --- CALENDAR CROSS-REFERENCE ---
        # If yfinance per-ticker API missed this ticker's earnings, but the
        # Yahoo earnings calendar scrape has it, inject the dates.  This
        # catches cases where t.earnings_dates is stale/empty/wrong.
        if earnings_calendar and ticker in earnings_calendar:
            _cal_dates = earnings_calendar[ticker]
            if isinstance(_cal_dates, str):
                _cal_dates = [_cal_dates]
            _cal_today = pd.Timestamp.now().normalize()
            for _cd_str in _cal_dates:
                _cd = pd.Timestamp(_cd_str).normalize()
                earnings_dates.add(_cd)
                if not recent_earnings:
                    _cd_diff = (_cal_today - _cd).days
                    if 0 <= _cd_diff <= 7:
                        recent_earnings = True

        # Tag move_info with earnings EARLY — before any early returns
        # (fixes bug where 0-signal movers slipped through missed-mover earnings filter)
        if move_info is not None:
            move_info["earnings_driven"] = recent_earnings
            # Also compute days_since for display in missed movers report
            if earnings_dates:
                _mi_today = pd.Timestamp.now().normalize()
                _mi_sorted = sorted(earnings_dates)
                _mi_past = [d for d in _mi_sorted if d < _mi_today]
                _mi_future = [d for d in _mi_sorted if d >= _mi_today]
                move_info["earn_days_since"] = (_mi_today - _mi_past[-1]).days if _mi_past else None
                move_info["earn_days_until"] = (_mi_future[0] - _mi_today).days if _mi_future else None

        # Relative Strength vs SPY (no API call — reuses prefetched spy_df)
        rs_raw = None
        if spy_df is not None and not spy_df.empty:
            rs_raw = calculate_relative_strength(df, spy_df)

        # VCP forming → developing watchlist (even if no other signals)
        if vcp["state"] == "forming":
            developing.append({
                "ticker": ticker,
                "price": current_price,
                "pivot": vcp["pivot"],
                "dist_pct": vcp["dist_pct"],
                "contractions": vcp["contractions"],
                "sector": "Unknown",  # deferred — not worth API call for watchlist
                "rs_raw": rs_raw,
            })

        # DIP WATCH — stocks extended below key MAs (pre-signal watchlist)
        # Fires even without enough signals for a pick — "heads up, this is dipping"
        # Only for stocks that were ABOVE the MA recently (pullback, not breakdown)
        if len(df) >= 200:
            _dw_ema200 = calculate_200ma(df)
            _dw_ema21 = calculate_ema(df, 21)
            _dw_ema5 = calculate_ema(df, 5)
            _dw_200 = float(_dw_ema200.iloc[-1]) if not pd.isna(_dw_ema200.iloc[-1]) else None
            _dw_21 = float(_dw_ema21.iloc[-1]) if not pd.isna(_dw_ema21.iloc[-1]) else None
            _dw_5 = float(_dw_ema5.iloc[-1]) if not pd.isna(_dw_ema5.iloc[-1]) else None

            _dw_dips = {}
            # Check each MA: price below it AND was above it within last 20 bars
            if _dw_200 and current_price < _dw_200:
                _dist = (current_price - _dw_200) / _dw_200 * 100
                # Was above 200 EMA within last 20 bars? (pullback, not long-term breakdown)
                _was_above = any(float(df["Close"].iloc[i]) > float(_dw_ema200.iloc[i])
                                 for i in range(-20, -1) if not pd.isna(_dw_ema200.iloc[i]))
                if _dist <= -2.0 and _was_above:
                    _dw_dips["200"] = round(_dist, 1)

            if _dw_21 and current_price < _dw_21:
                _dist = (current_price - _dw_21) / _dw_21 * 100
                _was_above = any(float(df["Close"].iloc[i]) > float(_dw_ema21.iloc[i])
                                 for i in range(-15, -1) if not pd.isna(_dw_ema21.iloc[i]))
                if _dist <= -3.0 and _was_above:
                    _dw_dips["21"] = round(_dist, 1)

            if _dw_5 and current_price < _dw_5:
                _dist = (current_price - _dw_5) / _dw_5 * 100
                if _dist <= -2.0:
                    _dw_dips["5"] = round(_dist, 1)

            # Need at least 1 MA dip for watchlist (lower bar than signal which needs 2)
            if _dw_dips:
                # Lightweight earnings proximity from prefetched dates (no API call)
                _dw_earn = None
                if earnings_dates:
                    _dw_today = pd.Timestamp.now().normalize()
                    _dw_sorted = sorted(earnings_dates)
                    _dw_future = [d for d in _dw_sorted if d >= _dw_today]
                    _dw_past = [d for d in _dw_sorted if d < _dw_today]
                    _dw_days_until = (_dw_future[0] - _dw_today).days if _dw_future else None
                    _dw_days_since = (_dw_today - _dw_past[-1]).days if _dw_past else None
                    if _dw_days_until is not None or _dw_days_since is not None:
                        _dw_earn = {"days_until": _dw_days_until, "days_since": _dw_days_since}
                dip_watch.append({
                    "ticker": ticker,
                    "price": round(float(current_price), 2),
                    "dips": _dw_dips,
                    "rs_raw": rs_raw,
                    "n_signals": len(signals),
                    "earnings": _dw_earn,
                })

        # Volume ratio — compute for ALL tickers (needed for leaderboard even if no signals)
        vol_cfg = config.get("volume", {})
        vol_ratio = calculate_volume_ratio(df, vol_cfg.get("avg_period", 20))

        # Lightweight earnings for leaderboard (same approach as dip_watch)
        _lb_earn = None
        if earnings_dates:
            _lb_today = pd.Timestamp.now().normalize()
            _lb_sorted = sorted(earnings_dates)
            _lb_future = [d for d in _lb_sorted if d >= _lb_today]
            _lb_past = [d for d in _lb_sorted if d < _lb_today]
            _lb_days_until = (_lb_future[0] - _lb_today).days if _lb_future else None
            _lb_days_since = (_lb_today - _lb_past[-1]).days if _lb_past else None
            _lb_next_date = _lb_future[0].strftime("%m/%d") if _lb_future else None
            _lb_last_date = _lb_past[-1].strftime("%m/%d") if _lb_past else None
            if _lb_days_until is not None or _lb_days_since is not None:
                _lb_earn = {"days_until": _lb_days_until, "days_since": _lb_days_since,
                            "next_date": _lb_next_date, "last_date": _lb_last_date}

        # Attach EPS actual vs expected to earnings dict
        if _lb_earn and eps_data:
            _lb_earn["eps"] = eps_data
        elif eps_data:
            _lb_earn = {"days_until": None, "days_since": None, "eps": eps_data}

        # Leaderboard entry — raw per-signal data for ALL tickers, before confluence filter
        leaderboard_entry = {
            "ticker": ticker,
            "price": round(float(current_price), 2),
            "signals": [dict(s) for s in signals],
            "vol_ratio": vol_ratio,
            "rs_raw": rs_raw,
            "metrics": sig_metrics,
            "trend_200": trend_200,
            "earnings": _lb_earn,
            "revenue": rev_data,
        }

        if not signals:
            return [], developing, move_info, dip_watch, approaching_raw, leaderboard_entry

        bullish = [s for s in signals if s["direction"] == "bullish"]
        bearish = [s for s in signals if s["direction"] == "bearish"]

        # Update move_info with signal counts and technical fingerprint
        if move_info is not None:
            move_info["signal_count"] = len(signals)
            move_info["bull_signals"] = len(bullish)
            move_info["bear_signals"] = len(bearish)
            move_info["signals_fired"] = [s["indicator"] for s in signals]
            move_info["earnings_driven"] = recent_earnings
            try:
                move_info["fingerprint"] = _compute_mover_fingerprint(
                    df, signals, trend_200
                )
            except Exception:
                move_info["fingerprint"] = {}

        # Snapshot opposing signal data BEFORE contradiction resolution clears lists.
        # Even when the 60% rule discards weaker signals, they still represent
        # real technical headwinds that should dampen scores.
        _opposing_pre = {
            "bullish": {
                "weight": sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bearish),
                "count": len(bearish),
                "names": [s["indicator"] for s in bearish],
            },
            "bearish": {
                "weight": sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bullish),
                "count": len(bullish),
                "names": [s["indicator"] for s in bullish],
            },
        }

        # Resolve contradictory signals — only discard the weaker direction
        # when it's clearly outweighed (< 60% of stronger side's weight).
        # When it's close, evaluate each direction independently so stocks
        # like ALGN (4bull/3bear) don't lose a valid setup to elimination.
        if len(bullish) >= 2 and len(bearish) >= 2:
            bull_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bullish)
            bear_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bearish)
            stronger_wt = max(bull_wt, bear_wt)
            weaker_wt = min(bull_wt, bear_wt)
            if weaker_wt < stronger_wt * 0.6:
                # Clear winner — discard the weak side
                if bull_wt >= bear_wt:
                    bearish = []
                else:
                    bullish = []
            # Otherwise: both directions evaluated independently below

        # Early exit: if neither direction has enough confluence, skip API calls
        if not _passes_confluence(bullish) and not _passes_confluence(bearish):
            return [], developing, move_info, dip_watch, approaching_raw, leaderboard_entry

        # Only fetch expensive data for qualifying tickers (~50 vs 505)
        # Live price: always fetch for qualifying tickers
        # fast_info during market hours (fast), t.info after hours (has extended prices)
        live = get_live_price(ticker, extended_hours=data_is_stale)
        if live is not None:
            current_price = live
        # After hours: re-run signals with live price so "above/below X" checks are current
        if data_is_stale and live is not None:
            signals, trend_200, _, recent_earnings, earnings_dates, approaching_raw, sig_metrics, eps_data, rev_data = generate_signals(
                df, config, ticker=ticker, live_price=current_price, distrib_threshold=distrib_threshold
            )
            # Update leaderboard entry with live-price signals
            leaderboard_entry.update({
                "price": round(float(current_price), 2),
                "signals": [dict(s) for s in signals],
                "metrics": sig_metrics,
                "trend_200": trend_200,
            })
            bullish = [s for s in signals if s["direction"] == "bullish"]
            bearish = [s for s in signals if s["direction"] == "bearish"]
            # Re-snapshot opposing data with live-price signals
            _opposing_pre = {
                "bullish": {
                    "weight": sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bearish),
                    "count": len(bearish),
                    "names": [s["indicator"] for s in bearish],
                },
                "bearish": {
                    "weight": sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bullish),
                    "count": len(bullish),
                    "names": [s["indicator"] for s in bullish],
                },
            }
            # Re-apply contradiction resolution
            if len(bullish) >= 2 and len(bearish) >= 2:
                bull_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bullish)
                bear_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in bearish)
                if min(bull_wt, bear_wt) < max(bull_wt, bear_wt) * 0.6:
                    if bull_wt >= bear_wt:
                        bearish = []
                    else:
                        bullish = []
            # Re-check confluence — live price may change which signals fire
                if not _passes_confluence(bullish) and not _passes_confluence(bearish):
                    return [], developing, move_info, dip_watch, approaching_raw, leaderboard_entry

        info = get_ticker_info(ticker)
        fundamentals = get_fundamentals(ticker)
        intel = get_market_intel(ticker)
        htf_trend = get_higher_timeframe_trend(ticker, daily_df=df)

        # Attach value grade to leaderboard entry (available for featured plays)
        if leaderboard_entry is not None and fundamentals:
            leaderboard_entry["value_grade"] = score_value(fundamentals, "bullish")

        # Sector rotation status
        sector_status = None
        sector_rank = None
        if sector_rotation and info["sector"] in sector_rotation:
            sec_data = sector_rotation[info["sector"]]
            sector_status = sec_data.get("status")
            sector_rank = sec_data.get("rank")

        # --- RECENT EARNINGS FLAG ---
        # If earnings happened in the last 7 days, signals may just reflect
        # the earnings gap.  Still score & display the pick, but mark it so
        # it is NOT saved as a tracked pick (no adaptive learning).
        # Also catch live/premarket gaps vs close — a 3%+ move overnight
        # strongly suggests earnings/news even if the API missed it.
        _earnings_recent = recent_earnings
        close_price = float(df["Close"].iloc[-1])
        if not _earnings_recent and live is not None:
            live_gap = abs(live - close_price) / close_price
            if live_gap >= 0.02:
                _earnings_recent = True
                # Inject today as approximate event date for days_since display
                earnings_dates.add(pd.Timestamp.now().normalize())

        for direction, group in [("bullish", bullish), ("bearish", bearish)]:
            if not _passes_confluence(group):
                continue

            # Opposing signal check — trade direction must dominate
            opposing = len(bearish) if direction == "bullish" else len(bullish)
            if opposing >= len(group):
                continue  # Evenly split or opposed — no directional edge

            score, score_breakdown = calculate_score(group, direction, trend_200, htf_trend, market_trends, vol_ratio, df, adaptive_weights, pillar_mults=pillar_mults, rs_raw=rs_raw, regime=regime)

            # Setup context — counter-trend plays got discounted trend credit in scoring
            setup_context = _get_setup_context(group)

            # --- CONFLICTING SIGNAL PENALTY ---
            # Even when the 60% rule discards the weaker direction, the opposing
            # signals still represent real technical headwinds.
            # Penalty scales proportionally: more opposing weight = bigger penalty.
            opp_data = _opposing_pre.get(direction, {"weight": 0, "count": 0, "names": []})
            opposing_weight = opp_data["weight"]
            opposing_names = opp_data["names"]
            opposing_count = opp_data["count"]
            conflict_penalty = 0.0
            dir_weight = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in group)
            if opposing_weight > 0 and dir_weight > 0:
                conflict_ratio = min(opposing_weight / dir_weight, 1.0)
                conflict_penalty = conflict_ratio * CONFLICTING_SIGNAL_MAX_PENALTY
                score -= conflict_penalty
                score = max(score, 0.0)

            # --- MARKET INTELLIGENCE ADJUSTMENT ---
            intel_adj = compute_intel_adjustment(intel, direction, current_price)
            intel_total = intel_adj.get("total", 0.0)
            if intel_total != 0:
                score += intel_total
                score = max(score, 0.0)

            # --- OVERBOUGHT / EXTENDED PENALTY (with-trend entries only) ---
            # When chasing a trend-continuation setup into overbought/extended territory,
            # mean reversion probability increases. Counter-trend plays are exempt
            # because they're betting ON the reversal.
            _ob_extended_applied = False
            if setup_context == "with_trend":
                _rsi_series = calculate_rsi(df, 14)
                _rsi_val = float(_rsi_series.iloc[-1]) if not pd.isna(_rsi_series.iloc[-1]) else 50.0
                _ma50 = df["Close"].rolling(50).mean()
                _std50 = df["Close"].rolling(50).std()
                _z_score = 0.0
                if (len(df) >= 55 and not pd.isna(_ma50.iloc[-1])
                        and not pd.isna(_std50.iloc[-1]) and _std50.iloc[-1] > 0):
                    _z_score = (float(current_price) - float(_ma50.iloc[-1])) / float(_std50.iloc[-1])

                if direction == "bullish":
                    _is_overbought = _rsi_val >= 70
                    _is_extended = _z_score >= 2.0
                else:
                    _is_overbought = _rsi_val <= 30
                    _is_extended = _z_score <= -2.0

                if _is_overbought and _is_extended:
                    score -= OVERBOUGHT_EXTENDED_PENALTY
                    _ob_extended_applied = True
                elif _is_overbought:
                    score -= OVERBOUGHT_ONLY_PENALTY
                    _ob_extended_applied = True
                elif _is_extended:
                    score -= EXTENDED_ONLY_PENALTY
                    _ob_extended_applied = True
                score = max(score, 0.0)

            # --- SECTOR HARD GATE ---
            # Top-down: don't fight sector momentum (Macro → Sector → Stock)
            if sector_status == "lagging" and direction == "bullish":
                # Exception: bottom reversal playbook
                # Sector stabilizing (1d or 10d positive) + counter-trend setup
                sec_data = sector_rotation.get(info["sector"], {}) if sector_rotation else {}
                stabilizing = sec_data.get("ret_1d", -99) > 0 or sec_data.get("ret_10d", -99) > 0
                if not (stabilizing and setup_context == "counter_trend"):
                    continue  # Don't long in a lagging sector

            if sector_status == "leading" and direction == "bearish":
                # Exception: sector topping (1d negative) + counter-trend setup
                sec_data = sector_rotation.get(info["sector"], {}) if sector_rotation else {}
                topping = sec_data.get("ret_1d", 99) < 0
                if not (topping and setup_context == "counter_trend"):
                    continue  # Don't short in a leading sector

            # --- REGIME FILTER ---
            indicator_names = [s["indicator"] for s in group]
            structure_signals = {"Support/Resist", "EMA21 Pullback", "200 MA", "MA Cross", "VCP Breakout", "Breakout Retest", "Channel Bounce", "Golden Cross", "Failed Breakdown", "Failed Breakout", "Double Bottom", "Double Top", "Higher Lows", "Dist Breakdown"}
            has_structure = any(n in structure_signals for n in indicator_names)

            if regime == "chop":
                is_trend_play = setup_context == "with_trend"

                # Structure gate — pure oscillator setups are noise in chop
                if not has_structure:
                    continue

                # Volume filter — conviction volume matters more in chop
                # Hard reject only at very low volume; otherwise penalize score
                vol_min = CHOP_TREND_VOL_MIN if is_trend_play else CHOP_VOL_MIN
                if vol_ratio < 0.7:
                    continue  # dangerously low volume — skip entirely
                elif vol_ratio < vol_min:
                    # Proportional penalty: further below threshold = bigger penalty
                    # At vol_min (1.3): 0 penalty, at 0.7: -1.5 penalty
                    vol_gap = (vol_min - vol_ratio) / (vol_min - 0.7)
                    score -= 1.5 * vol_gap

                # Trend play discount — no trend to continue in chop
                if is_trend_play:
                    score *= CHOP_TREND_PLAY_DISCOUNT

                # Range play boost — applied BEFORE compression so it lifts the base
                range_count = sum(1 for n in indicator_names if n in RANGE_PLAY_SIGNALS)
                if range_count >= 2:
                    score += CHOP_RANGE_BOOST

                # Soft compression: squeeze B-tier+ range into [5.5, cap]
                # Preserves rank order instead of flattening everything to the cap
                if score > TIER_B_MIN:
                    excess = score - TIER_B_MIN
                    max_excess = 10.0 - TIER_B_MIN
                    compressed = (REGIME_CHOP_CAP - TIER_B_MIN) * (excess / max_excess)
                    score = TIER_B_MIN + compressed
                score = min(score, REGIME_CHOP_CAP)

            # Market disagreement cap — prevent A-tier picks against SPY/QQQ
            mkt_aligned = market_agrees(market_trends, direction)
            # Rotation divergence override: when system detects internal breakdown,
            # trades aligned with the divergence get a higher cap (200 MA is lagging)
            _divergence_override = False
            _rot_active = (rotation_divergence and rotation_divergence.get("divergent")
                           and rotation_divergence.get("momentum") != "unwinding")
            if not mkt_aligned and _rot_active:
                mkt_dirs = [t for t in market_trends.values() if t != "neutral"]
                mkt_dir = mkt_dirs[0] if mkt_dirs else "neutral"
                if (mkt_dir == "bullish" and direction == "bearish") or \
                   (mkt_dir == "bearish" and direction == "bullish"):
                    _divergence_override = True
                    score = min(score, DIVERGENCE_MKT_CAP)
                else:
                    score = min(score, MKT_DISAGREE_CAP)
            elif not mkt_aligned:
                score = min(score, MKT_DISAGREE_CAP)

            # Rotation divergence penalty — market/sector brains disagree
            # Skip penalty when rotation is unwinding (growth reclaiming)
            rotation_warning = None
            if _rot_active:
                # Bullish market + defensive rotation → penalize offensive longs
                # Bearish market + offensive rotation → penalize defensive shorts
                mkt_dirs = [t for t in market_trends.values() if t != "neutral"]
                mkt_dir = mkt_dirs[0] if mkt_dirs else "neutral"
                sector_name = info["sector"]
                if (mkt_dir == "bullish" and direction == "bullish"
                        and sector_name in OFFENSIVE_SECTORS):
                    score -= 0.5
                    rotation_warning = rotation_divergence["reason"]
                elif (mkt_dir == "bearish" and direction == "bearish"
                      and sector_name in DEFENSIVE_SECTORS):
                    score -= 0.5
                    rotation_warning = rotation_divergence["reason"]

            tier = get_tier(score)
            if not tier:
                continue  # Below C-tier, don't show

            # Trade type (Law 2)
            trend_agrees = (
                (direction == "bullish" and trend_200 == "bullish") or
                (direction == "bearish" and trend_200 == "bearish")
            )
            htf_aligned = (htf_trend == "neutral" or htf_trend == direction)

            if trend_agrees and mkt_aligned and htf_aligned:
                trade_type = "SWING"
            elif trend_agrees and mkt_aligned:
                trade_type = "SWING (weekly caution)"
            elif trend_agrees:
                trade_type = "WATCH (market disagrees)"
            elif mkt_aligned:
                trade_type = "WATCH (against 200 MA)"
            else:
                trade_type = "WATCH (counter-trend)"

            # WATCH trades: still show in report but don't track as picks —
            # no intraday data to manage the elevated risk properly
            if trade_type.startswith("WATCH") and score < 4.5:
                continue

            # Trade plan (initial — risk tier will be updated after EV calc)
            trade_plan = generate_trade_plan(df, direction, score, trade_type,
                                             stop_mult=stop_mult,
                                             live_price=current_price)

            # Value grade — fundamentals as a separate lens (not mixed into score)
            value_data = score_value(fundamentals, direction)

            # Play type classification (needed before preference boost + result dict)
            play_type, play_tags = classify_play(group, direction, sector_status, rs_raw)

            # User preference boost — how well this pick matches user's favorites
            pref_boost = 0.0
            if user_prefs:
                pref_dict = {
                    "play_type": play_type,
                    "play_tags": play_tags,
                    "signals": group,
                    "setup_context": setup_context,
                    "direction": direction,
                    "sector_status": sector_status,
                }
                pref_boost = compute_preference_boost(pref_dict, user_prefs)

            # --- 52-WEEK HIGH + OVERBOUGHT FLAG ---
            # Chasing into all-time-highs with exhaustion signals = elevated risk
            near_52w_high_overbought = False
            if direction == "bullish" and len(df) >= 252:
                _high_52w = float(df["High"].rolling(min(252, len(df))).max().iloc[-1])
                if _high_52w > 0:
                    _proximity = (_high_52w - float(current_price)) / _high_52w
                    if _proximity <= 0.02:  # within 2% of 52-week high
                        _ob_signals = {"RSI", "Mean Reversion", "Bollinger", "Stochastic"}
                        _has_ob = any(n in _ob_signals for n in opposing_names)
                        if not _has_ob:
                            _rsi_chk = calculate_rsi(df, 14)
                            _rsi_now = float(_rsi_chk.iloc[-1]) if not pd.isna(_rsi_chk.iloc[-1]) else 50.0
                            _has_ob = _rsi_now >= 70
                        near_52w_high_overbought = _has_ob

            # Expected Value (with contextual win rate + RS + sector adjustment)
            # setup_context makes sector adj play-type aware:
            # momentum plays benefit from leading sectors,
            # mean-reversion plays benefit from lagging sectors
            ev = calculate_ev(
                score, trade_plan["risk"], trade_plan["tp1"], trade_plan["tp2"],
                trade_plan["entry"], direction,
                vol_ratio=vol_ratio, trend_agrees=trend_agrees,
                mkt_aligned=mkt_aligned, htf_agrees=htf_aligned,
                rs_raw=rs_raw, sector_status=sector_status,
                adaptive_wr=adaptive_wr, regime=regime,
                setup_context=setup_context,
                preference_boost=pref_boost,
                has_opposing_signals=bool(opposing_count > 0),
                near_52w_high_overbought=near_52w_high_overbought,
                divergence_override=_divergence_override,
                intel=intel,
            )

            # --- EV HARD GATE ON TIERS ---
            if ev["ev_ratio"] < EV_TIER_C_MAX:
                tier = "C"
            elif ev["ev_ratio"] < EV_TIER_A_MIN and tier == "A":
                tier = "B"

            # --- MARKET HEADWIND TIER DEMOTION ---
            # Going against SPY/QQQ = one tier harder to earn
            _headwind = not mkt_aligned and not _divergence_override
            if _headwind:
                if tier == "A":
                    tier = "B"
                elif tier == "B":
                    tier = "C"

            # Value grade is informational only — displayed in report but does
            # not adjust score or tier.  Technical setup drives the trade.

            # Re-evaluate risk tier with EV data (EV gate + Kelly)
            trade_plan["risk_tier"] = get_risk_tier(
                score, trade_type, ev["ev_ratio"], ev["win_rate"],
                avg_win=ev.get("avg_win", 0), avg_loss=ev.get("avg_loss", 0)
            )

            # --- EARNINGS PROXIMITY CHECK ---
            earnings = check_earnings_proximity(ticker, prefetched_dates=earnings_dates,
                                                earnings_calendar=earnings_calendar)
            if earnings["days_until"] is not None and earnings["days_until"] <= EARNINGS_SKIP_DAYS:
                continue  # Skip — binary event, stops are meaningless
            if earnings["days_until"] is not None and earnings["days_until"] <= EARNINGS_WARN_DAYS:
                trade_plan["risk_tier"] = {
                    "size": "QUARTER 0.25%",
                    "desc": f"Earnings in {earnings['days_until']}d — forced QUARTER"
                }

            # Trend strength for transparency
            trend_strength = check_trend_strength(
                group, direction, trend_200, htf_trend, market_trends, df
            )

            # Turnaround plays are position trades, not swings
            if play_type == "turnaround":
                trade_type = "POSITION (turnaround)"

            results.append({
                "ticker": ticker,
                "price": current_price,
                "direction": direction,
                "trade_type": trade_type,
                "play_type": play_type,
                "play_tags": play_tags,
                "setup_context": setup_context,
                "signals": [dict(s) for s in group],  # deep copy — prevent reference mutation
                "signal_count": len(group),
                "trend_200": trend_200,
                "htf_trend": htf_trend,
                "vol_ratio": vol_ratio,
                "sector": info["sector"],
                "industry": info["industry"],
                "score": round(score, 1),
                "tier": tier,
                "trend_strength": trend_strength,
                "plan": trade_plan,
                "ev": ev,
                "preference_boost": round(pref_boost * 100, 1),
                "rs_raw": rs_raw,
                "earnings": earnings,
                "sector_status": sector_status,
                "sector_rank": sector_rank,
                "rotation_warning": rotation_warning,
                "opposing_count": opposing_count,
                "opposing_weight": round(opposing_weight, 2),
                "opposing_names": opposing_names,
                "conflict_penalty": round(conflict_penalty, 2),
                "near_52w_high_overbought": near_52w_high_overbought,
                "divergence_override": _divergence_override,
                "headwind": _headwind,
                "value_grade": value_data,
                "intel": intel,
                "intel_adj": intel_adj,
                "earnings_recent": _earnings_recent,
                "breakdown": score_breakdown,
            })

    except Exception as e:
        print(f"    ERROR: {ticker} — {e}")

    # Quality-filter approaching items: only keep for tickers with 2+ signals
    if approaching_raw and len(signals) >= 2:
        approaching_items = approaching_raw

    return results, developing, move_info, dip_watch, approaching_items, leaderboard_entry


def classify_play(signals: list, direction: str, sector_status: str = None,
                  rs_raw: float = None) -> tuple:
    """Classify signals into a named play type. Returns (primary, all_tags).
    Primary is the play with the highest adaptive win rate (if data exists),
    otherwise falls back to priority order (first match)."""
    indicators = {s["indicator"] for s in signals}
    tags = []

    # Collect ALL matching play types (priority order — first match = primary label)
    if "VCP Breakout" in indicators:
        tags.append("vcp_breakout")

    if "Failed Breakdown" in indicators:
        tags.append("failed_breakdown")
    if "Failed Breakout" in indicators:
        tags.append("failed_breakout")

    if "Golden Cross" in indicators:
        gc_dir = next((s["direction"] for s in signals if s["indicator"] == "Golden Cross"), "")
        tags.append("golden_cross" if gc_dir == "bullish" else "death_cross")

    if "Double Bottom" in indicators:
        tags.append("double_bottom")
    if "Double Top" in indicators:
        tags.append("double_top")
    if "M on Uptrend" in indicators:
        tags.append("m_on_uptrend")
    if "W on Downtrend" in indicators:
        tags.append("w_on_downtrend")
    if "M on Downtrend" in indicators:
        tags.append("m_on_downtrend")
    if "M Breakout Up" in indicators:
        tags.append("m_breakout_up")
    if "M Breakout Down" in indicators:
        tags.append("m_breakout_down")

    if "Turnaround" in indicators and direction == "bullish":
        tags.append("turnaround")

    # Channel takes priority over accumulation/distribution — the channel
    # defines the actual trade (entry at boundary, stop beyond, target opposite).
    # Accumulation/distribution become supporting evidence, not the thesis.
    if "Trendline Channel" in indicators:
        tc_dir = next((s["direction"] for s in signals if s["indicator"] == "Trendline Channel"), "")
        tags.append("channel_support" if tc_dir == "bullish" else "channel_resist")

    if "Trendline Break" in indicators:
        tb_dir = next((s["direction"] for s in signals if s["indicator"] == "Trendline Break"), "")
        tags.append("descending_trendline_breakout" if tb_dir == "bullish" else "ascending_trendline_breakdown")

    if "Accumulation" in indicators:
        tags.append("accumulation")
    if "Distribution" in indicators:
        tags.append("distribution")

    if "Early Accumulation" in indicators:
        tags.append("early_accumulation")
    if "Early Distribution" in indicators:
        tags.append("early_distribution")

    if "Range Compression" in indicators:
        tags.append("range_compression")

    if "Dist Breakdown" in indicators:
        tags.append("lower_highs_dist")

    if "Higher Lows" in indicators:
        tags.append("higher_lows")

    if "Post-Earnings" in indicators:
        pe_dir = next((s["direction"] for s in signals if s["indicator"] == "Post-Earnings"), "")
        tags.append("post_earnings" if pe_dir == "bullish" else "post_earnings_bear")

    if "Breakout Retest" in indicators and direction == "bullish":
        tags.append("breakout_retest")

    if "Trend Accel" in indicators:
        tags.append("trend_accel")

    if (sector_status == "leading" and rs_raw is not None and rs_raw > 5
            and direction == "bullish"
            and ("EMA21 Pullback" in indicators or "Support/Resist" in indicators)):
        tags.append("sector_ride")

    # RS Continuation — individual stock leader pulling back (classification only, no signal)
    if (rs_raw is not None and rs_raw > 10 and direction == "bullish"
            and ("EMA21 Pullback" in indicators or "Support/Resist" in indicators)
            and "sector_ride" not in tags):
        tags.append("rs_continuation")

    if "200 MA" in indicators:
        ma_detail = next((s["detail"] for s in signals if s["indicator"] == "200 MA"), "")
        tags.append("ma200_reclaim" if "Reclaimed" in ma_detail else "ma200_loss")

    if "RSI" in indicators:
        rsi_detail = next((s["detail"] for s in signals if s["indicator"] == "RSI"), "")
        if "oversold" in rsi_detail and ("Support/Resist" in indicators or "Stochastic" in indicators):
            tags.append("oversold_bounce")
        if "overbought" in rsi_detail:
            tags.append("resistance_reject")

    if "Mean Reversion" in indicators:
        tags.append("mean_reversion")

    if "RSI Divergence" in indicators:
        tags.append("rsi_divergence")

    if "Volume Climax" in indicators:
        tags.append("volume_climax")

    if "Gap Fill" in indicators:
        tags.append("gap_fill")

    if "Channel Bounce" in indicators:
        tags.append("channel_bounce")

    if "Deep Pullback" in indicators:
        tags.append("deep_pullback")

    if "Earnings Beat Drift" in indicators:
        tags.append("earnings_beat_drift")
    if "Earnings Overreaction" in indicators:
        tags.append("earnings_overreaction")
    if "Earnings Anticipation" in indicators:
        tags.append("earnings_anticipation")

    if "EMA21 Pullback" in indicators:
        tags.append("ema21_pullback")

    if "5 EMA Touch" in indicators:
        tags.append("5_ema_touch")
    if "200 MA Touch" in indicators:
        tags.append("200_ma_touch")

    if "MACD" in indicators:
        tags.append("macd_momentum")

    if "Support/Resist" in indicators and "support_bounce" not in tags and "resistance_reject" not in tags:
        sr_detail = next((s["detail"] for s in signals if s["indicator"] == "Support/Resist"), "")
        tags.append("support_bounce" if "Bounce" in sr_detail else "resistance_reject")

    if not tags:
        tags.append("multi_signal")

    # Primary = play type with highest adaptive win rate (if sufficient data).
    # Falls back to priority order (tags[0]) for cold-start or low-sample plays.
    primary = tags[0]
    if len(tags) > 1:
        wr = _get_play_type_wr_cached()
        if wr:
            rated = [(t, wr[t]) for t in tags if t in wr]
            if rated:
                best = max(rated, key=lambda x: x[1])
                primary = best[0]
    return primary, tags
