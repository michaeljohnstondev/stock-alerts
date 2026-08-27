import pandas as pd
import numpy as np
import yfinance as yf

from .indicators import (
    calculate_rsi, calculate_macd, calculate_moving_averages,
    calculate_bollinger_bands, calculate_200ma, calculate_ema,
    calculate_stochastic, calculate_adx, calculate_atr, detect_vcp,
    find_nearest_support, find_nearest_resistance, calculate_volume_ratio,
)


# ---------------------------------------------------------------------------
# Approaching setup detection — patterns not yet triggered
# ---------------------------------------------------------------------------

from .constants import (
    APPROACHING_THRESHOLDS, APPROACHING_MIN_DISTANCE_PCT, APPROACHING_PLAY_MAP,
)


def detect_approaching_setups(price, ema21_val, ma200_val, support, resistance,
                               best_chan_any, partial_dbl_bottom,
                               atr_val, atr_pct, ticker=""):
    """Detect setups approaching trigger levels. Uses data already computed in generate_signals().
    Returns list of approaching item dicts."""
    items = []
    if price <= 0 or atr_val <= 0:
        return items

    _min_dist = APPROACHING_MIN_DISTANCE_PCT
    _t = APPROACHING_THRESHOLDS

    # --- MA Convergence: price approaching EMA21 or 200 MA ---
    for ma_val, ma_label, approach_type in [
        (ema21_val, "EMA21", "ema21_approach"),
        (ma200_val, "200 MA", "ma200_approach"),
    ]:
        if ma_val is None or ma_val <= 0:
            continue
        dist_pct = (price - ma_val) / ma_val
        abs_dist = abs(dist_pct)
        dist_atr = abs(price - ma_val) / atr_val if atr_val > 0 else 999

        if _min_dist < abs_dist <= _t["ema_convergence_pct"] and dist_atr <= _t["ema_convergence_atr"]:
            side = "from above" if dist_pct > 0 else "from below"
            items.append({
                "type": approach_type,
                "likely_play": APPROACHING_PLAY_MAP.get(approach_type, "multi_signal"),
                "level": round(ma_val, 2),
                "level_label": ma_label,
                "distance_pct": round(abs_dist * 100, 1),
                "detail": f"approaching {ma_label} (${ma_val:.2f}), {abs_dist*100:.1f}% {side}",
                "ticker": ticker,
            })

    # --- Support / Resistance Approach ---
    if support > 0:
        dist_pct = (price - support) / support
        dist_atr = (price - support) / atr_val if atr_val > 0 else 999
        if _min_dist < dist_pct <= _t["support_resist_pct"] and dist_atr <= _t["support_resist_atr"]:
            items.append({
                "type": "support_approach",
                "likely_play": APPROACHING_PLAY_MAP["support_approach"],
                "level": round(support, 2),
                "level_label": "Support",
                "distance_pct": round(dist_pct * 100, 1),
                "detail": f"approaching support ${support:.2f}, {dist_pct*100:.1f}% away",
                "ticker": ticker,
            })

    if resistance > 0:
        dist_pct = (resistance - price) / resistance
        dist_atr = (resistance - price) / atr_val if atr_val > 0 else 999
        if _min_dist < dist_pct <= _t["support_resist_pct"] and dist_atr <= _t["support_resist_atr"]:
            items.append({
                "type": "resist_approach",
                "likely_play": APPROACHING_PLAY_MAP["resist_approach"],
                "level": round(resistance, 2),
                "level_label": "Resistance",
                "distance_pct": round(dist_pct * 100, 1),
                "detail": f"approaching resistance ${resistance:.2f}, {dist_pct*100:.1f}% away",
                "ticker": ticker,
            })

    # --- Double Bottom Forming: two matching lows, no bounce confirmed ---
    if partial_dbl_bottom and not partial_dbl_bottom.get("_used"):
        low2 = partial_dbl_bottom["low2"]
        spread = partial_dbl_bottom["spread_pct"]
        neckline = partial_dbl_bottom["neckline_pct"]
        dist_from_low = (price - low2) / low2 * 100 if low2 > 0 else 0
        items.append({
            "type": "dbl_bottom_forming",
            "likely_play": APPROACHING_PLAY_MAP["dbl_bottom_forming"],
            "level": low2,
            "level_label": "Double Bottom",
            "distance_pct": round(abs(dist_from_low), 1),
            "detail": f"double bottom forming at ${partial_dbl_bottom['low1']:.2f}/${low2:.2f} "
                      f"({spread:.1f}% spread, {neckline:.0f}% neck), price {dist_from_low:+.1f}% from low",
            "ticker": ticker,
        })

    # --- Channel Boundary Approach: price nearing trendline support/resistance ---
    if best_chan_any:
        proj_sup = best_chan_any["proj_sup"]
        proj_res = best_chan_any["proj_res"]
        ch_atr = best_chan_any.get("atr", atr_val)

        # Approaching channel support from above
        dist_to_sup = price - proj_sup
        if ch_atr > 0 and 0.75 < dist_to_sup / ch_atr <= _t["channel_boundary_atr"]:
            items.append({
                "type": "channel_sup_approach",
                "likely_play": APPROACHING_PLAY_MAP["channel_sup_approach"],
                "level": proj_sup,
                "level_label": f"{best_chan_any['ch_dir'].title()} Channel Support",
                "distance_pct": round(dist_to_sup / price * 100, 1),
                "detail": f"approaching {best_chan_any['ch_dir']} channel support ~${proj_sup:.2f}, "
                          f"{dist_to_sup/ch_atr:.1f} ATR away",
                "ticker": ticker,
            })

        # Approaching channel resistance from below
        dist_to_res = proj_res - price
        if ch_atr > 0 and 0.75 < dist_to_res / ch_atr <= _t["channel_boundary_atr"]:
            items.append({
                "type": "channel_res_approach",
                "likely_play": APPROACHING_PLAY_MAP["channel_res_approach"],
                "level": proj_res,
                "level_label": f"{best_chan_any['ch_dir'].title()} Channel Resist",
                "distance_pct": round(dist_to_res / price * 100, 1),
                "detail": f"approaching {best_chan_any['ch_dir']} channel resistance ~${proj_res:.2f}, "
                          f"{dist_to_res/ch_atr:.1f} ATR away",
                "ticker": ticker,
            })

    return items


# ---------------------------------------------------------------------------
# Signal generation — all indicators
# ---------------------------------------------------------------------------

def generate_signals(df: pd.DataFrame, config: dict, ticker: str = "",
                     live_price: float = None, distrib_threshold: float = 0.77):
    """Returns (signals_list, trend_200_str, vcp_dict, had_recent_earnings_bool, earnings_dates_set, approaching_list, metrics_dict)."""
    signals = []
    recent_earnings = False  # set True if earnings happened in last 7 calendar days
    earnings_dates = set()   # populated by post-earnings check, reusable by caller
    ind = config["indicators"]
    price = live_price if live_price is not None else df["Close"].iloc[-1]

    # ATR for threshold normalization (Phase 1)
    _atr_series = calculate_atr(df)
    _atr_val = float(_atr_series.iloc[-1]) if len(_atr_series) > 0 and not pd.isna(_atr_series.iloc[-1]) else price * 0.02
    _atr_pct = _atr_val / price if price > 0 else 0.02

    # 200 MA
    ma200 = calculate_200ma(df)
    ma200_val = ma200.iloc[-1] if not pd.isna(ma200.iloc[-1]) else None
    trend_200 = "neutral"
    if ma200_val is not None:
        trend_200 = "bullish" if price > ma200_val else "bearish"

    # --- RSI (weight 1.0) ---
    _rsi_current = None  # saved for metrics export
    if ind["rsi"]["enabled"]:
        rsi = calculate_rsi(df, ind["rsi"]["period"])
        val = rsi.iloc[-1]
        if not pd.isna(val):
            _rsi_current = round(float(val), 1)
        if val >= ind["rsi"]["overbought"]:
            _rsi_strength = round(min(1.0, max(0.3, (val - ind["rsi"]["overbought"]) / 15)), 2)
            signals.append({"indicator": "RSI", "direction": "bearish",
                            "detail": f"RSI {val:.1f} (overbought)",
                            "strength": _rsi_strength,
                            "meta": {"value": round(float(val), 1),
                                     "threshold": ind["rsi"]["overbought"],
                                     "period": ind["rsi"]["period"],
                                     "zone": "overbought"}})
        elif val <= ind["rsi"]["oversold"]:
            _rsi_strength = round(min(1.0, max(0.3, (ind["rsi"]["oversold"] - val) / 15)), 2)
            signals.append({"indicator": "RSI", "direction": "bullish",
                            "detail": f"RSI {val:.1f} (oversold)",
                            "strength": _rsi_strength,
                            "meta": {"value": round(float(val), 1),
                                     "threshold": ind["rsi"]["oversold"],
                                     "period": ind["rsi"]["period"],
                                     "zone": "oversold"}})

    # --- MACD (weight 1.0) ---
    # 3-bar lookback: a crossover that held 2-3 days is MORE confirmed than day-1.
    if ind["macd"]["enabled"]:
        mc = ind["macd"]
        _, _, hist = calculate_macd(df, mc["fast_period"], mc["slow_period"], mc["signal_period"])
        if len(hist) >= 4:
            # Bullish: histogram positive now, find when it crossed (within 3 bars)
            if hist.iloc[-1] > 0:
                _cross_age = 0
                for _lb in range(2, min(5, len(hist) + 1)):
                    if hist.iloc[-_lb] <= 0:
                        _cross_age = _lb - 1
                        break
                if 1 <= _cross_age <= 3:
                    _macd_strength = round({1: 0.5, 2: 0.8}.get(_cross_age, 1.0), 2)
                    _held = f" (held {_cross_age}d)" if _cross_age > 1 else ""
                    signals.append({"indicator": "MACD", "direction": "bullish",
                                    "detail": f"Bullish crossover{_held}",
                                    "strength": _macd_strength,
                                    "meta": {"hist_now": round(float(hist.iloc[-1]), 4),
                                             "hist_prev": round(float(hist.iloc[-2]), 4),
                                             "cross_age": _cross_age,
                                             "fast": mc["fast_period"], "slow": mc["slow_period"],
                                             "signal": mc["signal_period"]}})
            # Bearish: histogram negative now, find when it crossed (within 3 bars)
            elif hist.iloc[-1] < 0:
                _cross_age = 0
                for _lb in range(2, min(5, len(hist) + 1)):
                    if hist.iloc[-_lb] >= 0:
                        _cross_age = _lb - 1
                        break
                if 1 <= _cross_age <= 3:
                    _macd_strength = round({1: 0.5, 2: 0.8}.get(_cross_age, 1.0), 2)
                    _held = f" (held {_cross_age}d)" if _cross_age > 1 else ""
                    signals.append({"indicator": "MACD", "direction": "bearish",
                                    "detail": f"Bearish crossover{_held}",
                                    "strength": _macd_strength,
                                    "meta": {"hist_now": round(float(hist.iloc[-1]), 4),
                                             "hist_prev": round(float(hist.iloc[-2]), 4),
                                             "cross_age": _cross_age,
                                             "fast": mc["fast_period"], "slow": mc["slow_period"],
                                             "signal": mc["signal_period"]}})

    # --- MA Cross 20/50 — Law 7 held 2 closes (weight 0.75) ---
    if ind["moving_averages"]["enabled"]:
        mc = ind["moving_averages"]
        sma_s, sma_l = calculate_moving_averages(df, mc["short_period"], mc["long_period"])
        if len(sma_s) >= 3 and len(sma_l) >= 3:
            if (sma_s.iloc[-1] > sma_l.iloc[-1] and sma_s.iloc[-2] > sma_l.iloc[-2]
                    and sma_s.iloc[-3] <= sma_l.iloc[-3]):
                spread_pct = (float(sma_s.iloc[-1]) - float(sma_l.iloc[-1])) / float(sma_l.iloc[-1]) * 100
                _ma_strength = round(min(1.0, max(0.4, abs(spread_pct) / 2.0)), 2)
                signals.append({"indicator": "MA Cross", "direction": "bullish",
                                "detail": f"SMA{mc['short_period']} > SMA{mc['long_period']} (held 2d)",
                                "strength": _ma_strength,
                                "meta": {"short_period": mc["short_period"], "long_period": mc["long_period"],
                                         "sma_short": round(float(sma_s.iloc[-1]), 2),
                                         "sma_long": round(float(sma_l.iloc[-1]), 2),
                                         "spread_pct": round(spread_pct, 2)}})
            elif (sma_s.iloc[-1] < sma_l.iloc[-1] and sma_s.iloc[-2] < sma_l.iloc[-2]
                    and sma_s.iloc[-3] >= sma_l.iloc[-3]):
                spread_pct = (float(sma_s.iloc[-1]) - float(sma_l.iloc[-1])) / float(sma_l.iloc[-1]) * 100
                _ma_strength = round(min(1.0, max(0.4, abs(spread_pct) / 2.0)), 2)
                signals.append({"indicator": "MA Cross", "direction": "bearish",
                                "detail": f"SMA{mc['short_period']} < SMA{mc['long_period']} (held 2d)",
                                "strength": _ma_strength,
                                "meta": {"short_period": mc["short_period"], "long_period": mc["long_period"],
                                         "sma_short": round(float(sma_s.iloc[-1]), 2),
                                         "sma_long": round(float(sma_l.iloc[-1]), 2),
                                         "spread_pct": round(spread_pct, 2)}})

    # --- Bollinger Bands (weight 0.5) ---
    if ind["bollinger_bands"]["enabled"]:
        bc = ind["bollinger_bands"]
        upper, _, lower = calculate_bollinger_bands(df, bc["period"], bc["std_dev"])
        if price <= lower.iloc[-1]:
            _bb_band_val = float(lower.iloc[-1])
            _bb_strength = round(min(1.0, max(0.3, abs(price - _bb_band_val) / _atr_val * 0.3 + 0.3)), 2)
            signals.append({"indicator": "Bollinger", "direction": "bullish",
                            "detail": f"${price:.2f} at lower band ${lower.iloc[-1]:.2f}",
                            "strength": _bb_strength,
                            "meta": {"price": round(price, 2), "upper": round(float(upper.iloc[-1]), 2),
                                     "lower": round(float(lower.iloc[-1]), 2),
                                     "period": bc["period"], "std_dev": bc["std_dev"], "band": "lower"}})
        elif price >= upper.iloc[-1]:
            _bb_band_val = float(upper.iloc[-1])
            _bb_strength = round(min(1.0, max(0.3, abs(price - _bb_band_val) / _atr_val * 0.3 + 0.3)), 2)
            signals.append({"indicator": "Bollinger", "direction": "bearish",
                            "detail": f"${price:.2f} at upper band ${upper.iloc[-1]:.2f}",
                            "strength": _bb_strength,
                            "meta": {"price": round(price, 2), "upper": round(float(upper.iloc[-1]), 2),
                                     "lower": round(float(lower.iloc[-1]), 2),
                                     "period": bc["period"], "std_dev": bc["std_dev"], "band": "upper"}})

    # --- 200 MA reclaim/loss — Law 3/7 (weight 1.0) ---
    if ma200_val is not None and len(ma200) >= 3 and not pd.isna(ma200.iloc[-3]):
        above_now = price > ma200.iloc[-1]
        above_prev = df["Close"].iloc[-2] > ma200.iloc[-2]
        above_prev2 = df["Close"].iloc[-3] > ma200.iloc[-3]
        if above_now and above_prev and not above_prev2:
            dist_pct = (price - ma200_val) / ma200_val * 100
            _ma200_strength = round(min(1.0, max(0.5, abs(dist_pct) / 3.0 * 0.5 + 0.5)), 2)
            signals.append({"indicator": "200 MA", "direction": "bullish",
                            "detail": f"Reclaimed ${ma200_val:.2f} (held 2d)",
                            "strength": _ma200_strength,
                            "meta": {"ma_val": round(float(ma200_val), 2), "price": round(price, 2),
                                     "distance_pct": round(dist_pct, 2), "action": "reclaimed"}})
        elif not above_now and not above_prev and above_prev2:
            dist_pct = (price - ma200_val) / ma200_val * 100
            _ma200_strength = round(min(1.0, max(0.5, abs(dist_pct) / 3.0 * 0.5 + 0.5)), 2)
            signals.append({"indicator": "200 MA", "direction": "bearish",
                            "detail": f"Lost ${ma200_val:.2f} (held 2d)",
                            "strength": _ma200_strength,
                            "meta": {"ma_val": round(float(ma200_val), 2), "price": round(price, 2),
                                     "distance_pct": round(dist_pct, 2), "action": "lost"}})

    # --- EMA21 Pullback (weight 0.85) ---
    # Widened: 6-bar window catches multi-day pullbacks (2-5 days), not just overnight dips.
    # Bar -6 must be above/below EMA21 (confirms prior trend).
    # Bars -5 through -2: any bar closing within 0.5% of EMA21 counts as a touch.
    # Today (bar -1): price must be back on the trend side of EMA21 (bounce confirmed).
    ema21 = calculate_ema(df, 21)
    _ema_touch_tol = 1.0 + max(0.003, min(0.01, 0.3 * _atr_pct))
    _ema_bounce_min = max(0.002, 0.2 * _atr_pct)
    if len(ema21) >= 6 and not pd.isna(ema21.iloc[-6]):
        # Bullish: was above EMA21, pulled back to it, now bouncing
        if price > ema21.iloc[-1]:
            pre_window_above = df["Close"].iloc[-6] > ema21.iloc[-6]
            touch_bars = 0
            deepest_bar_idx = -2
            deepest_ratio = 999.0
            for bi in range(-5, -1):
                bar_close = float(df["Close"].iloc[bi])
                bar_ema = float(ema21.iloc[bi])
                ratio = bar_close / bar_ema if bar_ema > 0 else 999.0
                if ratio <= _ema_touch_tol:
                    touch_bars += 1
                    if ratio < deepest_ratio:
                        deepest_ratio = ratio
                        deepest_bar_idx = bi
            bounce_strength = (price / float(ema21.iloc[-1])) - 1.0 if float(ema21.iloc[-1]) > 0 else 0
            if pre_window_above and touch_bars >= 2 and bounce_strength >= _ema_bounce_min:
                _ema_strength = round(min(1.0, max(0.3, bounce_strength / (0.5 * _atr_pct) * 0.5 + 0.3)), 2)
                signals.append({"indicator": "EMA21 Pullback", "direction": "bullish",
                                "detail": f"Bounce off EMA21 ${ema21.iloc[-1]:.2f} ({touch_bars}d pullback, +{bounce_strength:.1%} bounce)",
                                "strength": _ema_strength,
                                "meta": {"ema_val": round(float(ema21.iloc[-1]), 2), "price": round(price, 2),
                                         "touch_bars": touch_bars,
                                         "deepest_close": round(float(df["Close"].iloc[deepest_bar_idx]), 2),
                                         "deepest_ema": round(float(ema21.iloc[deepest_bar_idx]), 2),
                                         "action": "bounce"}})

    # --- Bounce-already-happened gate for proximity signals ---
    # If price has rallied too far off the 3-bar low, the entry is gone.
    _recent_low = df["Low"].iloc[-3:].min() if len(df) >= 3 else price
    _rally_pct = (price - _recent_low) / _recent_low if _recent_low > 0 else 0
    _bounce_limit = max(0.015, 0.5 * _atr_pct)  # 1.5% floor, scales with vol
    _bounce_already = _rally_pct > _bounce_limit

    # --- 5 EMA Touch / 200 MA Touch ---
    # Separate proximity signals for each key EMA. Fire independently so a stock
    # near both the 5 EMA and 200 MA gets 2 hits in confluence.
    # 21 EMA proximity is handled by EMA21 Pullback.
    _ema_touch_tol = max(0.003, min(0.015, 0.5 * _atr_pct))
    _ema5 = calculate_ema(df, 5)
    for _et_name, _et_series, _et_label, _et_weight in [
        ("5 EMA Touch", _ema5, "EMA 5", 0.70),
        ("200 MA Touch", ma200, "200 MA", 0.85),
    ]:
        if len(_et_series) < 2 or pd.isna(_et_series.iloc[-1]):
            continue
        _et_val = float(_et_series.iloc[-1])
        if _et_val <= 0:
            continue
        _et_dist = abs(price - _et_val) / _et_val
        if _et_dist <= _ema_touch_tol and not _bounce_already:
            _et_prox = 1.0 - (_et_dist / _ema_touch_tol) if _ema_touch_tol > 0 else 1.0
            _et_strength = round(min(1.0, max(0.3, _et_prox * 0.7 + 0.3)), 2)
            signals.append({
                "indicator": _et_name, "direction": "bullish",
                "detail": f"Touching {_et_label} ${_et_val:.2f} ({_et_dist * 100:.1f}% away)",
                "strength": _et_strength,
                "meta": {
                    "ema_label": _et_label,
                    "ema_val": round(_et_val, 2),
                    "dist_pct": round(_et_dist * 100, 2),
                    "proximity_score": round(_et_prox, 2),
                }
            })

    # --- Support Bounce / Resistance Rejection (weight 0.75) ---
    # Law 4 & 8: trade the edges, best entries come from pullbacks
    _sr_tol = max(0.005, min(0.025, 0.5 * _atr_pct))
    support = 0.0
    resistance = 0.0
    if len(df) >= 5:
        support = find_nearest_support(df, price)
        resistance = find_nearest_resistance(df, price)
        prev_low = df["Low"].iloc[-2]
        prev_close = df["Close"].iloc[-2]

        # Bullish: price is near support (proximity, like EMA Touch)
        if support > 0:
            dist_to_support = (price - support) / support
            if -0.005 <= dist_to_support <= _sr_tol and not _bounce_already:
                _sr_strength = round(min(1.0, max(0.3, 1.0 - abs(dist_to_support) / (0.5 * _atr_pct) * 0.7)), 2)
                signals.append({"indicator": "Support/Resist", "direction": "bullish",
                                "detail": f"Near support ${support:.2f}",
                                "strength": _sr_strength,
                                "meta": {"level": round(float(support), 2), "type": "support",
                                         "distance_pct": round(dist_to_support * 100, 2),
                                         "price": round(price, 2)}})

        # Bearish: yesterday's high pushed within tolerance of resistance, today closed back down
        if resistance > 0:
            dist_to_resist = (df["High"].iloc[-2] - resistance) / resistance
            if -_sr_tol <= dist_to_resist <= 0.005 and price < prev_close:
                _sr_strength = round(min(1.0, max(0.3, 1.0 - abs(dist_to_resist) / (0.5 * _atr_pct) * 0.7)), 2)
                signals.append({"indicator": "Support/Resist", "direction": "bearish",
                                "detail": f"Rejected at resistance ${resistance:.2f}",
                                "strength": _sr_strength,
                                "meta": {"level": round(float(resistance), 2), "type": "resistance",
                                         "distance_pct": round(dist_to_resist * 100, 2),
                                         "price": round(price, 2)}})

    # --- Stochastic (weight 0.5) ---
    stoch_cfg = ind.get("stochastic", {"enabled": True, "k_period": 14, "d_period": 3})
    if stoch_cfg.get("enabled", True):
        k, d = calculate_stochastic(df, stoch_cfg.get("k_period", 14), stoch_cfg.get("d_period", 3))
        if len(k) >= 2 and len(d) >= 2:
            if k.iloc[-1] < 20 and k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]:
                _k_val = float(k.iloc[-1])
                _stoch_strength = round(min(1.0, max(0.3, (20 - _k_val) / 15)), 2)
                signals.append({"indicator": "Stochastic", "direction": "bullish",
                                "detail": f"Bullish cross in oversold (K={k.iloc[-1]:.0f})",
                                "strength": _stoch_strength,
                                "meta": {"k": round(float(k.iloc[-1]), 1), "d": round(float(d.iloc[-1]), 1),
                                         "k_prev": round(float(k.iloc[-2]), 1), "d_prev": round(float(d.iloc[-2]), 1),
                                         "zone": "oversold"}})
            elif k.iloc[-1] > 80 and k.iloc[-1] < d.iloc[-1] and k.iloc[-2] >= d.iloc[-2]:
                _k_val = float(k.iloc[-1])
                _stoch_strength = round(min(1.0, max(0.3, (_k_val - 80) / 15)), 2)
                signals.append({"indicator": "Stochastic", "direction": "bearish",
                                "detail": f"Bearish cross in overbought (K={k.iloc[-1]:.0f})",
                                "strength": _stoch_strength,
                                "meta": {"k": round(float(k.iloc[-1]), 1), "d": round(float(d.iloc[-1]), 1),
                                         "k_prev": round(float(k.iloc[-2]), 1), "d_prev": round(float(d.iloc[-2]), 1),
                                         "zone": "overbought"}})

    # --- ADX Trend Strength (weight 0.5) ---
    adx_cfg = ind.get("adx", {"enabled": True, "period": 14, "threshold": 25})
    if adx_cfg.get("enabled", True):
        adx = calculate_adx(df, adx_cfg.get("period", 14))
        adx_val = adx.iloc[-1]
        if not pd.isna(adx_val) and adx_val >= adx_cfg.get("threshold", 25):
            # ADX confirms a strong trend — direction comes from price vs 200 MA,
            # with short-term override for recovery stocks
            adx_direction = trend_200 if trend_200 != "neutral" else "bullish"
            # Short-term override: if 200 MA says bearish but 20 MA momentum is
            # bullish, the ADX is measuring recovery strength, not downtrend strength
            if adx_direction == "bearish" and len(df) >= 20:
                _ma20_adx = df["Close"].rolling(20).mean()
                if (not pd.isna(_ma20_adx.iloc[-1]) and not pd.isna(_ma20_adx.iloc[-5])
                        and _ma20_adx.iloc[-1] > _ma20_adx.iloc[-5]
                        and price > _ma20_adx.iloc[-1]):
                    adx_direction = "bullish"
            _adx_threshold = adx_cfg.get("threshold", 25)
            _adx_strength = round(min(1.0, max(0.3, (float(adx_val) - _adx_threshold) / 25)), 2)
            signals.append({"indicator": "ADX Trend", "direction": adx_direction,
                            "detail": f"ADX {adx_val:.1f} (strong trend)",
                            "strength": _adx_strength,
                            "meta": {"adx_val": round(float(adx_val), 1),
                                     "threshold": _adx_threshold}})

    # --- VCP Breakout (weight 1.0) — Minervini volatility contraction ---
    vcp = detect_vcp(df)
    if vcp["state"] == "breakout":
        signals.append({"indicator": "VCP Breakout", "direction": "bullish",
                        "detail": f"Breakout above ${vcp['pivot']:.2f} pivot ({vcp['contractions']} contractions, {vcp['vol_surge']:.1f}x vol)",
                        "strength": 0.7,
                        "meta": {"pivot": round(float(vcp["pivot"]), 2),
                                 "contractions": int(vcp["contractions"]),
                                 "vol_surge": round(float(vcp["vol_surge"]), 1),
                                 "price": round(price, 2)}})

    # --- Turnaround Recovery (weight 0.85) ---
    # Beaten-down stock >40% below 52-week high showing first signs of life
    if len(df) >= 60:
        high_52w = df["High"].rolling(min(252, len(df))).max().iloc[-1]
        if not pd.isna(high_52w) and high_52w > 0:
            drawdown = (price - high_52w) / high_52w
            if drawdown < -0.40:
                recent_20 = df.iloc[-20:]
                recent_low = recent_20["Low"].min()
                older_low = df.iloc[-40:-20]["Low"].min() if len(df) >= 40 else recent_low
                avg_vol_20 = df["Volume"].iloc[-20:].mean()
                cur_vol = df["Volume"].iloc[-1]
                vol_surge = cur_vol / avg_vol_20 if avg_vol_20 > 0 else 0
                if recent_low > older_low or vol_surge >= 1.3:
                    _ta_strength = round(min(1.0, max(0.4, abs(drawdown) / 0.5 * 0.3 + 0.4)), 2)
                    signals.append({"indicator": "Turnaround", "direction": "bullish",
                                    "detail": f"Down {drawdown*100:.0f}% from 52w high, higher low forming",
                                    "strength": _ta_strength,
                                    "meta": {"drawdown_pct": round(float(drawdown) * 100, 1),
                                             "high_52w": round(float(high_52w), 2),
                                             "recent_low": round(float(recent_low), 2),
                                             "older_low": round(float(older_low), 2),
                                             "vol_surge": round(float(vol_surge), 1),
                                             "price": round(price, 2)}})

    # --- Post-Earnings Pullback (weight 1.0) ---
    # Verify actual earnings happened near the gap date, not just any random gap
    eps_data = None
    if len(df) >= 22 and ticker:
        earnings_dates = set()
        try:
            t = yf.Ticker(ticker)
            edates = t.earnings_dates
            if edates is not None and not edates.empty:
                # Strip timezone to match df.index (tz-naive)
                today = pd.Timestamp.now().normalize()
                for ed in edates.index:
                    ed_naive = ed.tz_convert(None) if ed.tz is not None else ed
                    ed_naive = ed_naive.normalize()
                    earnings_dates.add(ed_naive)
                    # Flag if earnings happened in last 7 calendar days (~5 trading)
                    days_since = (today - ed_naive).days
                    if 0 <= days_since <= 7:
                        recent_earnings = True

                # Extract EPS actual vs expected from most recent past earnings
                _now_tz = pd.Timestamp.now(tz=edates.index.tz) if edates.index.tz else pd.Timestamp.now()
                _past_rows = edates[edates.index <= _now_tz]
                _future_rows = edates[edates.index > _now_tz]
                if not _past_rows.empty:
                    _last = _past_rows.iloc[0]
                    _act = _last.get("Reported EPS")
                    _est = _last.get("EPS Estimate")
                    _surp = _last.get("Surprise(%)")
                    if pd.notna(_act):
                        _ed_idx = _past_rows.index[0]
                        _ed_naive = _ed_idx.tz_convert(None) if _ed_idx.tz else _ed_idx
                        _ed_naive = _ed_naive.normalize()
                        # Next quarter's EPS estimate (from future earnings row)
                        _next_est = None
                        if not _future_rows.empty:
                            _next_row = _future_rows.iloc[-1]  # closest future date
                            _ne = _next_row.get("EPS Estimate")
                            if pd.notna(_ne):
                                _next_est = round(float(_ne), 2)
                        eps_data = {
                            "actual": round(float(_act), 2),
                            "estimate": round(float(_est), 2) if pd.notna(_est) else None,
                            "surprise_pct": round(float(_surp), 1) if pd.notna(_surp) else None,
                            "earn_date": _ed_naive.isoformat()[:10],
                            "next_estimate": _next_est,
                        }
                        # Historical drift: compare current earnings reaction to previous quarter's
                        # Expected move = (curr_surprise / prev_surprise) × prev_move
                        if eps_data.get("surprise_pct") is not None and pd.notna(_est):
                            _cmp = _ed_naive.tz_localize(df.index.tz) if df.index.tz else _ed_naive
                            _post = df[df.index >= _cmp]
                            if len(_post) >= 1:
                                _earn_price = float(_post["Close"].iloc[0])
                                _cur_price = float(df["Close"].iloc[-1])
                                _price_chg = ((_cur_price - _earn_price) / _earn_price) * 100
                                eps_data["price_at_earn"] = round(_earn_price, 2)
                                eps_data["price_chg_pct"] = round(_price_chg, 1)
                                # Earnings sensitivity from up to 4 prior quarters
                                # For each past quarter: sensitivity = move% / surprise%
                                # Average sensitivity × current surprise = expected move
                                _sensitivities = []
                                for _qi in range(1, min(len(_past_rows), 5)):
                                    _q_surp = _past_rows.iloc[_qi].get("Surprise(%)")
                                    if not pd.notna(_q_surp) or abs(float(_q_surp)) < 0.01:
                                        continue
                                    # Price at this quarter's earnings date
                                    _q_ed_idx = _past_rows.index[_qi]
                                    _q_ed_naive = _q_ed_idx.tz_convert(None) if _q_ed_idx.tz else _q_ed_idx
                                    _q_ed_naive = _q_ed_naive.normalize()
                                    _q_cmp = _q_ed_naive.tz_localize(df.index.tz) if df.index.tz else _q_ed_naive
                                    _q_post = df[df.index >= _q_cmp]
                                    if len(_q_post) < 1:
                                        continue
                                    _q_price = float(_q_post["Close"].iloc[0])
                                    # Price at the NEXT earnings date (one quarter later)
                                    _q_next_idx = _past_rows.index[_qi - 1]
                                    _q_next_naive = _q_next_idx.tz_convert(None) if _q_next_idx.tz else _q_next_idx
                                    _q_next_naive = _q_next_naive.normalize()
                                    _q_next_cmp = _q_next_naive.tz_localize(df.index.tz) if df.index.tz else _q_next_naive
                                    _q_next_post = df[df.index >= _q_next_cmp]
                                    if len(_q_next_post) < 1:
                                        continue
                                    _q_next_price = float(_q_next_post["Close"].iloc[0])
                                    _q_move = ((_q_next_price - _q_price) / _q_price) * 100
                                    _sensitivities.append(_q_move / float(_q_surp))
                                if _sensitivities:
                                    _sensitivities.sort()
                                    _mid = len(_sensitivities) // 2
                                    _avg_sens = _sensitivities[_mid] if len(_sensitivities) % 2 else (_sensitivities[_mid - 1] + _sensitivities[_mid]) / 2
                                    _curr_surp_f = eps_data["surprise_pct"]
                                    _expected_move = _avg_sens * _curr_surp_f
                                    eps_data["drift"] = round(_expected_move - _price_chg, 1)
                                    eps_data["expected_move"] = round(_expected_move, 1)
                                    eps_data["quarters_used"] = len(_sensitivities)
        except Exception:
            pass

        # --- Revenue-based fair value (Price/Revenue ratio) ---
        # Compute avg P/Rev ratio from past quarters, then FV = avg_ratio × latest revenue
        rev_data = None
        try:
            if ticker:
                _rv_t = yf.Ticker(ticker)
                _rv_qf = _rv_t.quarterly_financials
                if _rv_qf is not None and not _rv_qf.empty and "Total Revenue" in _rv_qf.index:
                    _rv_row = _rv_qf.loc["Total Revenue"].dropna()
                    if len(_rv_row) >= 2:
                        _rv_cur_price = float(df["Close"].iloc[-1])
                        _rv_ratios = []
                        for _rv_date, _rv_val in _rv_row.items():
                            _rv_val_f = float(_rv_val)
                            if _rv_val_f <= 0:
                                continue
                            # Get stock price near this quarter end date
                            _rv_ts = pd.Timestamp(_rv_date)
                            if df.index.tz:
                                _rv_ts = _rv_ts.tz_localize(df.index.tz) if _rv_ts.tz is None else _rv_ts
                            _rv_near = df[df.index <= _rv_ts]
                            if _rv_near.empty:
                                continue
                            _rv_price = float(_rv_near["Close"].iloc[-1])
                            _rv_ratios.append(_rv_price / _rv_val_f)
                        if len(_rv_ratios) >= 2:
                            _rv_ratios.sort()
                            _rv_mid = len(_rv_ratios) // 2
                            _rv_median = _rv_ratios[_rv_mid] if len(_rv_ratios) % 2 else (_rv_ratios[_rv_mid - 1] + _rv_ratios[_rv_mid]) / 2
                            _rv_latest = float(_rv_row.iloc[0])
                            _rv_fv = _rv_median * _rv_latest
                            _rv_gap = _rv_cur_price - _rv_fv
                            _rv_gap_pct = (_rv_gap / _rv_fv) * 100
                            rev_data = {
                                "fair_value": round(_rv_fv, 2),
                                "gap": round(_rv_gap, 2),
                                "gap_pct": round(_rv_gap_pct, 1),
                                "quarters_used": len(_rv_ratios),
                            }
        except Exception:
            pass

        # --- Earnings-based signals (use eps_data + earnings_dates) ---
        _today_ts = pd.Timestamp.now().normalize()

        # Earnings Beat Drift — beat but stock hasn't caught up (within 30d)
        if eps_data and eps_data.get("surprise_pct") is not None and eps_data["surprise_pct"] > 0:
            _ebd_date = eps_data.get("earn_date")
            _ebd_d = eps_data.get("drift")
            if _ebd_date and _ebd_d is not None and _ebd_d > 2:
                try:
                    _ebd_days = (pd.Timestamp.now().normalize() - pd.Timestamp(_ebd_date)).days
                except Exception:
                    _ebd_days = 999
                if _ebd_days <= 30:
                    _ebd_str = min(1.0, max(0.3, _ebd_d / 20.0))
                    signals.append({
                        "indicator": "Earnings Beat Drift",
                        "direction": "bullish",
                        "detail": f"Beat EPS {eps_data['surprise_pct']:+.1f}%, stock {eps_data.get('price_chg_pct', 0):+.1f}%, D:{_ebd_d:+.1f} upside left",
                        "strength": _ebd_str,
                        "meta": {
                            "eps_actual": eps_data.get("actual"),
                            "eps_estimate": eps_data.get("estimate"),
                            "surprise_pct": eps_data["surprise_pct"],
                            "drift": _ebd_d,
                            "expected_move": eps_data.get("expected_move"),
                            "price_chg_pct": eps_data.get("price_chg_pct", 0),
                            "days_since": _ebd_days,
                        }
                    })

        # Earnings Overreaction — missed but stock dropped way too much (within 30d)
        if eps_data and eps_data.get("surprise_pct") is not None and eps_data["surprise_pct"] < 0:
            _eor_date = eps_data.get("earn_date")
            _eor_d = eps_data.get("drift")
            if _eor_date and _eor_d is not None and _eor_d > 3:
                try:
                    _eor_days = (pd.Timestamp.now().normalize() - pd.Timestamp(_eor_date)).days
                except Exception:
                    _eor_days = 999
                if _eor_days <= 30:
                    _eor_str = min(1.0, max(0.3, _eor_d / 20.0))
                    signals.append({
                        "indicator": "Earnings Overreaction",
                        "direction": "bullish",
                        "detail": f"Missed EPS {eps_data['surprise_pct']:+.1f}%, stock {eps_data.get('price_chg_pct', 0):+.1f}%, D:{_eor_d:+.1f} oversold",
                        "strength": _eor_str,
                        "meta": {
                            "eps_actual": eps_data.get("actual"),
                            "eps_estimate": eps_data.get("estimate"),
                            "surprise_pct": eps_data["surprise_pct"],
                            "drift": _eor_d,
                            "expected_move": eps_data.get("expected_move"),
                            "price_chg_pct": eps_data.get("price_chg_pct", 0),
                            "days_since": _eor_days,
                        }
                    })

        # Earnings Anticipation — oversold stock with earnings 3-14 days away
        if earnings_dates and _rsi_current is not None and _rsi_current < 35:
            _ea_future = sorted(d for d in earnings_dates if d >= _today_ts)
            if _ea_future:
                _ea_days_until = (_ea_future[0] - _today_ts).days
                if 3 <= _ea_days_until <= 14:
                    _ea_raw = (35 - _rsi_current) / 25.0
                    _ea_str = min(1.0, max(0.3, _ea_raw))
                    _ea_prior_beat = bool(eps_data and eps_data.get("surprise_pct", 0) > 0)
                    signals.append({
                        "indicator": "Earnings Anticipation",
                        "direction": "bullish",
                        "detail": f"RSI {_rsi_current:.0f} oversold, earnings in {_ea_days_until}d{' (prior beat)' if _ea_prior_beat else ''}",
                        "strength": _ea_str,
                        "meta": {
                            "days_until": _ea_days_until,
                            "rsi": _rsi_current,
                            "prior_beat": _ea_prior_beat,
                        }
                    })

        # Fallback: if earnings API failed, detect overnight gaps + volume spikes
        # as likely earnings/news events in the last 7 calendar days.
        # Triggers:  2.5%+ gap alone  OR  1.5%+ gap with 1.5x volume
        #            OR  any day with 3x+ avg volume (earnings w/o gap)
        if not recent_earnings and len(df) >= 22:
            avg_vol = df["Volume"].iloc[-22:-2].mean()
            for i in range(-5, 0):
                if abs(i) < len(df):
                    gap = abs(float(df["Open"].iloc[i]) - float(df["Close"].iloc[i - 1])) / float(df["Close"].iloc[i - 1])
                    day_vol = float(df["Volume"].iloc[i])
                    if (gap >= 0.025
                            or (gap >= 0.015 and avg_vol > 0 and day_vol > avg_vol * 1.5)
                            or (avg_vol > 0 and day_vol > avg_vol * 3)):
                        recent_earnings = True
                        # Inject the gap date into earnings_dates so
                        # check_earnings_proximity can compute days_since.
                        _gd = pd.Timestamp(df.index[i])
                        gap_date = (_gd.tz_convert(None) if _gd.tz is not None else _gd).normalize()
                        earnings_dates.add(gap_date)
                        break

        for i in range(-20, -1):
            if abs(i) >= len(df):
                continue
            gap = (df["Open"].iloc[i] - df["Close"].iloc[i - 1]) / df["Close"].iloc[i - 1]
            # Convert to naive date for timezone-safe comparison
            raw_date = df.index[i]
            gap_day = raw_date.tz_convert(None).normalize() if raw_date.tz else raw_date.normalize()

            # Check if earnings happened within 2 trading days of this gap
            is_earnings_gap = False
            for ed in earnings_dates:
                if abs((gap_day - ed).days) <= 3:  # 3 calendar days = ~2 trading days
                    is_earnings_gap = True
                    break

            if not is_earnings_gap:
                continue

            gap_low = df["Low"].iloc[i]
            gap_close = df["Close"].iloc[i]

            if gap > 0.03:  # Bullish: market approved (gap up)
                if price <= gap_close * 1.05 and price >= gap_low * 0.95:
                    signals.append({"indicator": "Post-Earnings", "direction": "bullish",
                                    "detail": f"Earnings gap-up, pullback to ${gap_low:.2f} (gap fill level)",
                                    "strength": 0.8,
                                    "meta": {"gap_pct": round(float(gap) * 100, 1), "gap_type": "up",
                                             "gap_bar": int(i), "gap_level": round(float(gap_low), 2),
                                             "price": round(price, 2)}})
                    break
            elif gap < -0.03:  # Bearish: market disapproved (gap down)
                gap_high = df["High"].iloc[i]
                if price >= gap_close * 0.95 and price <= gap_high * 1.05:
                    signals.append({"indicator": "Post-Earnings", "direction": "bearish",
                                    "detail": f"Earnings gap-down, bouncing into ${gap_high:.2f} (gap fill resistance)",
                                    "strength": 0.8,
                                    "meta": {"gap_pct": round(float(gap) * 100, 1), "gap_type": "down",
                                             "gap_bar": int(i), "gap_level": round(float(gap_high), 2),
                                             "price": round(price, 2)}})
                    break

    # --- Breakout Retest (weight 1.0) ---
    # Broke above resistance in last 10 days, now retesting it as support
    if len(df) >= 15:
        pre_breakout_df = df.iloc[:-10]
        if len(pre_breakout_df) >= 10:
            old_resistance = find_nearest_resistance(pre_breakout_df, price)
            if old_resistance is not None and old_resistance > 0:
                recent_10 = df.iloc[-10:]
                broke_above = (recent_10["Close"] > old_resistance * 1.02).any()
                _br_tol = max(0.008, min(0.025, 0.5 * _atr_pct))
                near_level = abs(price - old_resistance) / old_resistance < _br_tol
                bouncing = price > df["Close"].iloc[-2]
                if broke_above and near_level and bouncing:
                    dist_pct = (price - old_resistance) / old_resistance * 100
                    _br_strength = round(min(1.0, max(0.5, 1.0 - abs(price - old_resistance) / _atr_val)), 2)
                    signals.append({"indicator": "Breakout Retest", "direction": "bullish",
                                    "detail": f"Retesting breakout level ${old_resistance:.2f} as support",
                                    "strength": _br_strength,
                                    "meta": {"old_resistance": round(float(old_resistance), 2),
                                             "price": round(price, 2),
                                             "distance_pct": round(dist_pct, 2)}})

    # --- RSI Divergence (weight 0.85) ---
    # Bullish: price makes lower low but RSI makes higher low (hidden strength)
    # Bearish: price makes higher high but RSI makes lower high (hidden weakness)
    if ind["rsi"]["enabled"] and len(df) >= 30:
        rsi_series = calculate_rsi(df, ind["rsi"]["period"])
        if len(rsi_series) >= 20 and not rsi_series.iloc[-20:].isna().all():
            # Compare two windows: recent 10 bars vs prior 10 bars
            recent_price_low = df["Low"].iloc[-10:].min()
            prior_price_low = df["Low"].iloc[-20:-10].min()
            recent_rsi_low = rsi_series.iloc[-10:].min()
            prior_rsi_low = rsi_series.iloc[-20:-10].min()
            recent_price_high = df["High"].iloc[-10:].max()
            prior_price_high = df["High"].iloc[-20:-10].max()
            recent_rsi_high = rsi_series.iloc[-10:].max()
            prior_rsi_high = rsi_series.iloc[-20:-10].max()
            if (not pd.isna(recent_rsi_low) and not pd.isna(prior_rsi_low)
                    and recent_price_low < prior_price_low
                    and recent_rsi_low > prior_rsi_low + 2):
                _rsi_div_spread = float(recent_rsi_low - prior_rsi_low)
                _rsi_div_strength = round(min(1.0, max(0.4, abs(_rsi_div_spread) / 10)), 2)
                signals.append({"indicator": "RSI Divergence", "direction": "bullish",
                                "detail": f"Bullish divergence — price lower low, RSI higher low ({recent_rsi_low:.0f} vs {prior_rsi_low:.0f})",
                                "strength": _rsi_div_strength,
                                "meta": {"recent_price": round(float(recent_price_low), 2),
                                         "prior_price": round(float(prior_price_low), 2),
                                         "recent_rsi": round(float(recent_rsi_low), 1),
                                         "prior_rsi": round(float(prior_rsi_low), 1),
                                         "spread": round(float(recent_rsi_low - prior_rsi_low), 1)}})
            elif (not pd.isna(recent_rsi_high) and not pd.isna(prior_rsi_high)
                    and recent_price_high > prior_price_high
                    and recent_rsi_high < prior_rsi_high - 2):
                _rsi_div_spread = float(recent_rsi_high - prior_rsi_high)
                _rsi_div_strength = round(min(1.0, max(0.4, abs(_rsi_div_spread) / 10)), 2)
                signals.append({"indicator": "RSI Divergence", "direction": "bearish",
                                "detail": f"Bearish divergence — price higher high, RSI lower high ({recent_rsi_high:.0f} vs {prior_rsi_high:.0f})",
                                "strength": _rsi_div_strength,
                                "meta": {"recent_price": round(float(recent_price_high), 2),
                                         "prior_price": round(float(prior_price_high), 2),
                                         "recent_rsi": round(float(recent_rsi_high), 1),
                                         "prior_rsi": round(float(prior_rsi_high), 1),
                                         "spread": round(float(recent_rsi_high - prior_rsi_high), 1)}})

    # --- Gap Fill (weight 0.75) ---
    # Stock gapped in last 20 days, now approaching the fill level
    # Gap must exceed the stock's normal daily range (ATR) to be meaningful —
    # a 3% move on a stock that moves 4% daily is noise, not a gap.
    if len(df) >= 22:
        gap_found = False
        _atr14 = calculate_atr(df, period=14)
        _atr_val_gf = float(_atr14.iloc[-1]) if not pd.isna(_atr14.iloc[-1]) else 0.0
        _atr_pct_gf = _atr_val_gf / price if price > 0 and _atr_val_gf > 0 else 0.03
        _gap_min = max(0.03, _atr_pct_gf * 1.5)  # at least 3% AND at least 1.5x ATR
        for gi in range(-20, -1):
            if abs(gi) >= len(df):
                continue
            gap_pct = (df["Open"].iloc[gi] - df["Close"].iloc[gi - 1]) / df["Close"].iloc[gi - 1]
            # Bullish: gap down, approaching fill from below
            if gap_pct < -_gap_min:
                gap_top = df["Close"].iloc[gi - 1]  # pre-gap close = fill target
                if price < gap_top and price >= gap_top * 0.90:
                    dist_away = ((gap_top - price) / price) * 100
                    _gf_strength = round(min(1.0, max(0.3, 1.0 - abs(dist_away) / 5)), 2)
                    signals.append({"indicator": "Gap Fill", "direction": "bullish",
                                    "detail": f"Approaching gap fill at ${gap_top:.2f} ({dist_away:.1f}% away)",
                                    "strength": _gf_strength,
                                    "meta": {"target": round(float(gap_top), 2),
                                             "gap_pct": round(float(gap_pct) * 100, 1),
                                             "gap_bar": int(gi), "price": round(price, 2),
                                             "distance_pct": round(dist_away, 1)}})
                    gap_found = True
                    break
            # Bearish: gap up, approaching fill from above
            if gap_pct > _gap_min:
                gap_bottom = df["Close"].iloc[gi - 1]  # pre-gap close = fill target
                if price > gap_bottom and price <= gap_bottom * 1.10:
                    dist_away = ((price - gap_bottom) / price) * 100
                    _gf_strength = round(min(1.0, max(0.3, 1.0 - abs(dist_away) / 5)), 2)
                    signals.append({"indicator": "Gap Fill", "direction": "bearish",
                                    "detail": f"Approaching gap fill at ${gap_bottom:.2f} ({dist_away:.1f}% away)",
                                    "strength": _gf_strength,
                                    "meta": {"target": round(float(gap_bottom), 2),
                                             "gap_pct": round(float(gap_pct) * 100, 1),
                                             "gap_bar": int(gi), "price": round(price, 2),
                                             "distance_pct": round(dist_away, 1)}})
                    gap_found = True
                    break
        # no break needed — gap_found handled above

    # --- Volume Climax (weight 0.85) ---
    # Extreme volume spike (3x+) on a big red candle = potential capitulation / selling exhaustion
    if len(df) >= 25:
        avg_vol_20 = df["Volume"].iloc[-21:-1].mean()
        last_vol = df["Volume"].iloc[-1]
        last_change = (df["Close"].iloc[-1] - df["Open"].iloc[-1]) / df["Open"].iloc[-1] if df["Open"].iloc[-1] > 0 else 0
        prev_change = (df["Close"].iloc[-2] - df["Open"].iloc[-2]) / df["Open"].iloc[-2] if df["Open"].iloc[-2] > 0 else 0
        if avg_vol_20 > 0:
            vol_mult = last_vol / avg_vol_20
            # Bearish climax (selling exhaustion → bullish): huge red candle on 2.5x+ volume, next day starts recovering
            if vol_mult >= 2.5 and prev_change < -max(0.015, 0.7 * _atr_pct):
                # Check if today is showing recovery (green or at least not continuing down)
                if last_change > -0.005:
                    _vc_strength = round(min(1.0, max(0.4, (vol_mult - 2.0) / 3.0 * 0.6 + 0.4)), 2)
                    signals.append({"indicator": "Volume Climax", "direction": "bullish",
                                    "detail": f"Selling climax — {vol_mult:.1f}x avg volume, signs of exhaustion",
                                    "strength": _vc_strength,
                                    "meta": {"vol_mult": round(float(vol_mult), 1),
                                             "avg_vol": round(float(avg_vol_20)),
                                             "last_vol": round(float(last_vol)),
                                             "type": "selling_climax"}})
            # Bullish climax (buying exhaustion → bearish): huge green candle on 2.5x+ volume, may be blow-off top
            elif vol_mult >= 2.5 and prev_change > max(0.02, 1.0 * _atr_pct):
                if last_change < 0.005:
                    _vc_strength = round(min(1.0, max(0.4, (vol_mult - 2.0) / 3.0 * 0.6 + 0.4)), 2)
                    signals.append({"indicator": "Volume Climax", "direction": "bearish",
                                    "detail": f"Buying climax — {vol_mult:.1f}x avg volume, possible blow-off top",
                                    "strength": _vc_strength,
                                    "meta": {"vol_mult": round(float(vol_mult), 1),
                                             "avg_vol": round(float(avg_vol_20)),
                                             "last_vol": round(float(last_vol)),
                                             "type": "buying_climax"}})

    # --- Channel Bounce (weight 0.75) ---
    # Stock in a range (not trending hard), bouncing off bottom or top of 20-day channel
    if len(df) >= 25:
        ch_high = df["High"].iloc[-20:].max()
        ch_low = df["Low"].iloc[-20:].min()
        ch_range = ch_high - ch_low
        if ch_range > 0 and ch_low > 0:
            ch_width_pct = ch_range / ch_low
            # Only fire if the channel is meaningful (ATR-normalized range — not too tight, not too wide)
            _ch_min = max(0.015, 1.0 * _atr_pct)
            _ch_max = max(0.10, 5.0 * _atr_pct)
            if _ch_min <= ch_width_pct <= _ch_max:
                # Position within channel: 0 = bottom, 1 = top
                position_in_ch = (price - ch_low) / ch_range
                prev_position = (df["Close"].iloc[-2] - ch_low) / ch_range
                # Bullish: near bottom of channel and bouncing up
                if position_in_ch < 0.25 and price > df["Close"].iloc[-2]:
                    _cb_strength = round(min(1.0, max(0.3, 1.0 - position_in_ch / 0.25)), 2)
                    signals.append({"indicator": "Channel Bounce", "direction": "bullish",
                                    "detail": f"Bounce off channel bottom ${ch_low:.2f} (range ${ch_low:.2f}-${ch_high:.2f})",
                                    "strength": _cb_strength,
                                    "meta": {"ch_high": round(float(ch_high), 2), "ch_low": round(float(ch_low), 2),
                                             "width_pct": round(float(ch_width_pct) * 100, 1),
                                             "position_pct": round(float(position_in_ch) * 100, 1),
                                             "price": round(price, 2)}})
                # Bearish: near top of channel and turning down
                elif position_in_ch > 0.75 and price < df["Close"].iloc[-2]:
                    _cb_strength = round(min(1.0, max(0.3, (position_in_ch - 0.75) / 0.25)), 2)
                    signals.append({"indicator": "Channel Bounce", "direction": "bearish",
                                    "detail": f"Rejected at channel top ${ch_high:.2f} (range ${ch_low:.2f}-${ch_high:.2f})",
                                    "strength": _cb_strength,
                                    "meta": {"ch_high": round(float(ch_high), 2), "ch_low": round(float(ch_low), 2),
                                             "width_pct": round(float(ch_width_pct) * 100, 1),
                                             "position_pct": round(float(position_in_ch) * 100, 1),
                                             "price": round(price, 2)}})

    # --- Golden Cross / Death Cross (weight 1.0) ---
    # 50 MA crossing 200 MA — THE institutional signal
    if len(df) >= 205:
        ma50_gc = df["Close"].rolling(50).mean()
        ma200_gc = df["Close"].rolling(200).mean()
        if (not pd.isna(ma50_gc.iloc[-1]) and not pd.isna(ma200_gc.iloc[-1])
                and not pd.isna(ma50_gc.iloc[-3]) and not pd.isna(ma200_gc.iloc[-3])):
            # Golden cross: 50 MA crosses above 200 MA (held 2 closes)
            if (ma50_gc.iloc[-1] > ma200_gc.iloc[-1] and ma50_gc.iloc[-2] > ma200_gc.iloc[-2]
                    and ma50_gc.iloc[-3] <= ma200_gc.iloc[-3]):
                spread_pct = (float(ma50_gc.iloc[-1]) - float(ma200_gc.iloc[-1])) / float(ma200_gc.iloc[-1]) * 100
                _gc_strength = round(min(1.0, max(0.5, abs(spread_pct) / 3.0 * 0.5 + 0.5)), 2)
                signals.append({"indicator": "Golden Cross", "direction": "bullish",
                                "detail": f"50 MA crossed above 200 MA (Golden Cross)",
                                "strength": _gc_strength,
                                "meta": {"ma50": round(float(ma50_gc.iloc[-1]), 2),
                                         "ma200": round(float(ma200_gc.iloc[-1]), 2),
                                         "spread_pct": round(spread_pct, 2), "cross_type": "golden"}})
            # Death cross: 50 MA crosses below 200 MA (held 2 closes)
            elif (ma50_gc.iloc[-1] < ma200_gc.iloc[-1] and ma50_gc.iloc[-2] < ma200_gc.iloc[-2]
                    and ma50_gc.iloc[-3] >= ma200_gc.iloc[-3]):
                spread_pct = (float(ma50_gc.iloc[-1]) - float(ma200_gc.iloc[-1])) / float(ma200_gc.iloc[-1]) * 100
                _gc_strength = round(min(1.0, max(0.5, abs(spread_pct) / 3.0 * 0.5 + 0.5)), 2)
                signals.append({"indicator": "Golden Cross", "direction": "bearish",
                                "detail": f"50 MA crossed below 200 MA (Death Cross)",
                                "strength": _gc_strength,
                                "meta": {"ma50": round(float(ma50_gc.iloc[-1]), 2),
                                         "ma200": round(float(ma200_gc.iloc[-1]), 2),
                                         "spread_pct": round(spread_pct, 2), "cross_type": "death"}})

    # --- Failed Breakdown / Failed Breakout (weight 1.0) ---
    # Price breaks a level, traps traders, reverses — highest R:R trap play
    if len(df) >= 15:
        pre_break_df = df.iloc[:-5]
        if len(pre_break_df) >= 10:
            fb_support = find_nearest_support(pre_break_df, price)
            fb_resist = find_nearest_resistance(pre_break_df, price)
            recent_5 = df.iloc[-5:]
            avg_vol_fb = df["Volume"].iloc[-25:-5].mean() if len(df) >= 25 else df["Volume"].iloc[:-5].mean()
            recent_vol_fb = df["Volume"].iloc[-1]
            vol_confirm_fb = recent_vol_fb > avg_vol_fb * 1.3 if avg_vol_fb > 0 else False
            # Failed breakdown (bullish): broke below support then reclaimed
            # ATR-normalized break and reclaim thresholds
            _fb_break_mult = 1.0 - max(0.015, min(0.04, 0.8 * _atr_pct))
            _fb_reclaim_mult = 1.0 + max(0.01, min(0.025, 0.5 * _atr_pct))
            if fb_support is not None and fb_support > 0:
                broke_below = (recent_5["Low"] < fb_support * _fb_break_mult).any()
                reclaimed = price > fb_support * _fb_reclaim_mult
                if broke_below and reclaimed and vol_confirm_fb:
                    vol_m = recent_vol_fb / avg_vol_fb
                    signals.append({"indicator": "Failed Breakdown", "direction": "bullish",
                                    "detail": f"Failed break below ${fb_support:.2f}, reclaimed with {vol_m:.1f}x volume",
                                    "strength": 0.7,
                                    "meta": {"level": round(float(fb_support), 2),
                                             "vol_mult": round(float(vol_m), 1),
                                             "price": round(price, 2)}})
            # Failed breakout (bearish): broke above resistance then fell back
            # ATR-normalized break and rejection thresholds
            _fo_break_mult = 1.0 + max(0.015, min(0.04, 0.8 * _atr_pct))
            _fo_reject_mult = 1.0 - max(0.01, min(0.025, 0.5 * _atr_pct))
            if fb_resist is not None and fb_resist > 0:
                broke_above = (recent_5["High"] > fb_resist * _fo_break_mult).any()
                fell_back = price < fb_resist * _fo_reject_mult
                if broke_above and fell_back and vol_confirm_fb:
                    vol_m = recent_vol_fb / avg_vol_fb
                    signals.append({"indicator": "Failed Breakout", "direction": "bearish",
                                    "detail": f"Failed break above ${fb_resist:.2f}, rejected with {vol_m:.1f}x volume",
                                    "strength": 0.7,
                                    "meta": {"level": round(float(fb_resist), 2),
                                             "vol_mult": round(float(vol_m), 1),
                                             "price": round(price, 2)}})

    # --- Double Bottom / Double Top ---
    # Multi-window scanning catches patterns forming over different timeframes.
    # Flat Double Bottom/Top: classic reversal — two tests of the same level (ATR-normalized).
    _PATTERN_WINDOWS = [(20, 8), (40, 15), (60, 20), (80, 30)]
    _best_dbl_bottom = None
    _best_dbl_top = None
    _partial_dbl_bottom = None  # two matching lows but no bounce confirmation yet
    _db_spread_tol = max(0.01, min(0.04, 0.7 * _atr_pct))
    _db_neckline_min = max(0.03, min(0.08, 2.0 * _atr_pct))

    for _pw_total, _pw_split in _PATTERN_WINDOWS:
        if len(df) < _pw_total:
            continue
        _pw = df.iloc[-_pw_total:]
        _pw_first = df.iloc[-_pw_total:-_pw_split]
        _pw_second = df.iloc[-_pw_split:]
        _min_gap = _pw_total // 4

        # ---- Double Bottom (flat) ----
        _low1 = float(_pw_first["Low"].min())
        _low2 = float(_pw_second["Low"].min())
        _b1 = int(_pw_first["Low"].values.argmin())
        _b2 = len(_pw_first) + int(_pw_second["Low"].values.argmin())
        _gap = _b2 - _b1
        if _low1 > 0 and _gap >= _min_gap:
            _spread = (_low2 - _low1) / _low1
            _mid_high = float(_pw["High"].iloc[_b1:_b2].max()) if _b2 > _b1 else _low1
            _neckline = (_mid_high - min(_low1, _low2)) / min(_low1, _low2) if min(_low1, _low2) > 0 else 0

            if abs(_spread) < _db_spread_tol and _neckline >= _db_neckline_min:
                _db_meta = {"_q": _neckline, "low1": round(_low1, 2), "low2": round(_low2, 2),
                    "spread_pct": round(abs(_spread) * 100, 2), "neckline_pct": round(_neckline * 100, 1),
                    "mid_high": round(_mid_high, 2), "bar1": -_pw_total + _b1, "bar2": -_pw_total + _b2,
                    "price": round(price, 2), "window": _pw_total}
                if price > _low2 * 1.02:
                    if not _best_dbl_bottom or _neckline > _best_dbl_bottom["_q"]:
                        _best_dbl_bottom = _db_meta
                elif not _best_dbl_bottom:
                    # Pattern forming but no bounce yet — track for approaching detection
                    if not _partial_dbl_bottom or _neckline > _partial_dbl_bottom["_q"]:
                        _partial_dbl_bottom = _db_meta

        # ---- Double Top (flat) ----
        _high1 = float(_pw_first["High"].max())
        _high2 = float(_pw_second["High"].max())
        _b1 = int(_pw_first["High"].values.argmax())
        _b2 = len(_pw_first) + int(_pw_second["High"].values.argmax())
        _gap = _b2 - _b1
        if _high1 > 0 and _gap >= _min_gap:
            _spread = (_high2 - _high1) / _high1
            _mid_low = float(_pw["Low"].iloc[_b1:_b2].min()) if _b2 > _b1 else _high1
            _neckline = (max(_high1, _high2) - _mid_low) / max(_high1, _high2) if max(_high1, _high2) > 0 else 0

            if abs(_spread) < _db_spread_tol and _neckline >= _db_neckline_min and price < _high2 * 0.98:
                if not _best_dbl_top or _neckline > _best_dbl_top["_q"]:
                    _best_dbl_top = {"_q": _neckline, "high1": round(_high1, 2), "high2": round(_high2, 2),
                        "spread_pct": round(abs(_spread) * 100, 2), "neckline_pct": round(_neckline * 100, 1),
                        "mid_low": round(_mid_low, 2), "bar1": -_pw_total + _b1, "bar2": -_pw_total + _b2,
                        "price": round(price, 2), "window": _pw_total}

    # --- M on Uptrend / W on Downtrend (swing-point detection) ---
    # Proper temporal ordering using actual swing highs/lows:
    #   M shape: V1 → P1 → V2 → P2 → pullback  (ascending support through V1, V2)
    #   W shape: P1 → V1 → P2 → V2 → bounce     (descending resistance through P1, P2)
    _best_m_uptrend = None
    _best_w_downtrend = None
    _best_m_downtrend = None
    _best_m_breakout_up = None
    _best_m_breakout_down = None
    _m_vol_ratio = calculate_volume_ratio(df)

    # ATR for proximity — price must be within 1 ATR of projected trendline
    _tr_mw = pd.concat([df["High"] - df["Low"],
                         (df["High"] - df["Close"].shift(1)).abs(),
                         (df["Low"] - df["Close"].shift(1)).abs()], axis=1).max(axis=1)
    _atr_mw = float(_tr_mw.iloc[-14:].mean()) if len(df) >= 14 else price * 0.02

    for _lookback in [15, 25, 40, 60, 80]:
        if len(df) < _lookback + 5:
            continue
        _mw = df.iloc[-_lookback:]
        # Zigzag swing detection — percentage-based reversal threshold.
        # A swing only registers when price reverses by X% from the extreme.
        # Scales with window: short patterns (2-3 week) have smaller legs,
        # long patterns (2+ months) can have 20%+ swings.
        #   15bar → 2%   25bar → 3%   40bar → 6%   60bar → 9%   80bar → 12%
        _rev_pct = {15: 0.02, 25: 0.03, 40: 0.06, 60: 0.09, 80: 0.12}[_lookback]
        _min_rev = _rev_pct * price
        _zz_hi = float(_mw["High"].iloc[0])
        _zz_hi_bar = 0
        _zz_lo = float(_mw["Low"].iloc[0])
        _zz_lo_bar = 0
        _zz_dir = 0  # 0 = undecided, 1 = tracking up, -1 = tracking down
        _clean = []

        for _i in range(len(_mw)):
            _h = float(_mw["High"].iloc[_i])
            _l = float(_mw["Low"].iloc[_i])

            if _zz_dir == 0:  # undecided — find first significant move
                if _h > _zz_hi:
                    _zz_hi, _zz_hi_bar = _h, _i
                if _l < _zz_lo:
                    _zz_lo, _zz_lo_bar = _l, _i
                if _zz_hi - _l >= _min_rev:
                    _clean.append((_zz_hi_bar, _zz_hi, "H"))
                    _zz_dir = -1
                    _zz_lo, _zz_lo_bar = _l, _i
                elif _h - _zz_lo >= _min_rev:
                    _clean.append((_zz_lo_bar, _zz_lo, "L"))
                    _zz_dir = 1
                    _zz_hi, _zz_hi_bar = _h, _i
            elif _zz_dir == 1:  # tracking up, looking for peak
                if _h > _zz_hi:
                    _zz_hi, _zz_hi_bar = _h, _i
                if _zz_hi - _l >= _min_rev:
                    _clean.append((_zz_hi_bar, _zz_hi, "H"))
                    _zz_dir = -1
                    _zz_lo, _zz_lo_bar = _l, _i
            else:  # tracking down, looking for valley
                if _l < _zz_lo:
                    _zz_lo, _zz_lo_bar = _l, _i
                if _h - _zz_lo >= _min_rev:
                    _clean.append((_zz_lo_bar, _zz_lo, "L"))
                    _zz_dir = 1
                    _zz_hi, _zz_hi_bar = _h, _i

        # Scan for M on Uptrend: L-H-L-H with ascending properties
        #   V1(low) → P1(high) → V2(low) → P2(high), V2>V1, P2>P1
        for _ci in range(len(_clean) - 3):
            _s1, _s2, _s3, _s4 = _clean[_ci], _clean[_ci + 1], _clean[_ci + 2], _clean[_ci + 3]
            if not (_s1[2] == "L" and _s2[2] == "H" and _s3[2] == "L" and _s4[2] == "H"):
                continue
            _v1_bar, _v1_p = _s1[0], _s1[1]
            _p1_bar, _p1_p = _s2[0], _s2[1]
            _v2_bar, _v2_p = _s3[0], _s3[1]
            _p2_bar, _p2_p = _s4[0], _s4[1]

            if _v2_p <= _v1_p or _p2_p <= _p1_p:
                continue  # Need ascending valleys AND ascending peaks
            if _p2_bar < len(_mw) // 2:
                continue  # P2 must be in the recent half of the window
            _peak_spread = (_p2_p - _p1_p) / _p1_p
            if not (0.02 <= _peak_spread <= 0.12):
                continue  # Peaks 2-12% apart
            _dip = _p1_p - _v2_p  # Absolute dip between peaks in dollars
            _dip_pct = _dip / _p1_p
            if _dip < _atr_mw:
                continue  # Dip must be at least 1 ATR (visible on chart, not noise)

            # Trendline through V1 and V2, project to current bar
            _v_slope = (_v2_p - _v1_p) / (_v2_bar - _v1_bar)
            _proj = _v2_p + _v_slope * (len(_mw) - 1 - _v2_bar)
            if _proj <= 0:
                continue

            # Price must be at or above projected support, within 1.5 ATR
            # AND pulling back from P2 (not rallying)
            _pulling_back = price < _p2_p * 0.98
            if price >= _proj and price <= _proj + _atr_mw * 1.5 and _pulling_back:
                # --- Proximity score (50% weight): how close to trendline ---
                _dist_from_support = (price - _proj) / _atr_mw
                _proximity_score = max(0.0, 1.0 - _dist_from_support / 1.5)

                # --- Pattern definition score (35% weight) ---
                # Spread quality: triangular peak at 6% (geometric center of 2-12%)
                _spread_score = max(0.0, 1.0 - abs(_peak_spread - 0.06) / 0.06)
                # Dip depth: linear ramp from 1 ATR (gate floor) to 2.5 ATR
                _dip_atr = _dip / _atr_mw
                _dip_score = min(1.0, max(0.0, (_dip_atr - 1.0) / 1.5))
                _pattern_score = 0.6 * _spread_score + 0.4 * _dip_score

                # --- Trendline validation (15% weight): intermediate touches ---
                _touch_count = 0
                for _bi in range(_v2_bar + 1, len(_mw)):
                    _tl_val = _v2_p + _v_slope * (_bi - _v2_bar)
                    if _tl_val <= 0:
                        continue
                    _bar_low = float(_mw["Low"].iloc[_bi])
                    if abs(_bar_low - _tl_val) <= 0.5 * _atr_mw:
                        _touch_count += 1
                _validation_score = min(1.0, _touch_count * 0.5)

                # --- Combined quality (used for best-match selection) ---
                _raw_strength = (0.50 * _proximity_score +
                                 0.35 * _pattern_score +
                                 0.15 * _validation_score)
                _q = _raw_strength

                if not _best_m_uptrend or _q > _best_m_uptrend["_q"]:
                    _best_m_uptrend = {
                        "_q": _q,
                        "high1": round(_p1_p, 2), "high2": round(_p2_p, 2),
                        "entry_low": round(_v1_p, 2), "mid_low": round(_v2_p, 2),
                        "spread_pct": round(_peak_spread * 100, 1),
                        "neckline_pct": round(_dip_pct * 100, 1),
                        "bar1": -_lookback + _p1_bar, "bar2": -_lookback + _p2_bar,
                        "price": round(price, 2), "window": _lookback,
                        "projected_support": round(float(_proj), 2),
                        "proximity_atr": round(_dist_from_support, 2),
                        "proximity_score": round(_proximity_score, 2),
                        "spread_score": round(_spread_score, 2),
                        "dip_atr": round(_dip_atr, 1),
                        "dip_score": round(_dip_score, 2),
                        "pattern_score": round(_pattern_score, 2),
                        "tl_touches": _touch_count,
                        "validation_score": round(_validation_score, 2),
                    }

            # M Breakout Up: same ascending pattern, but price broke ABOVE P2
            # Volume is a scoring bonus, not a gate — breakouts often start
            # on normal volume with confirmation coming days later.
            elif price > _p2_p and price <= _p2_p * 1.10:
                _bk_pct = (price - _p2_p) / _p2_p
                _bk_proximity = max(0.0, 1.0 - _bk_pct / 0.10)
                _bk_vol_bonus = min(1.0, max(0.0, (_m_vol_ratio - 1.0) / 1.0))
                _dip_atr = _dip / _atr_mw
                _dip_score = min(1.0, max(0.0, (_dip_atr - 1.0) / 1.5))
                _spread_score = max(0.0, 1.0 - abs(_peak_spread - 0.06) / 0.06)
                _bk_pattern = 0.6 * _spread_score + 0.4 * _dip_score
                _bk_q = 0.45 * _bk_proximity + 0.25 * _bk_vol_bonus + 0.30 * _bk_pattern
                if not _best_m_breakout_up or _bk_q > _best_m_breakout_up["_q"]:
                    _best_m_breakout_up = {
                        "_q": _bk_q,
                        "p2": round(_p2_p, 2),
                        "breakout_pct": round(_bk_pct * 100, 1),
                        "vol_ratio": round(_m_vol_ratio, 2),
                        "v1": round(_v1_p, 2), "v2": round(_v2_p, 2),
                        "p1": round(_p1_p, 2),
                        "price": round(price, 2), "window": _lookback,
                        "pattern_type": "ascending",
                    }

        # Scan for W on Downtrend: H-L-H-L with descending properties
        #   P1(high) → V1(low) → P2(high) → V2(low), P2<P1, V2<V1
        for _ci in range(len(_clean) - 3):
            _s1, _s2, _s3, _s4 = _clean[_ci], _clean[_ci + 1], _clean[_ci + 2], _clean[_ci + 3]
            if not (_s1[2] == "H" and _s2[2] == "L" and _s3[2] == "H" and _s4[2] == "L"):
                continue
            _p1_bar, _p1_p = _s1[0], _s1[1]
            _v1_bar, _v1_p = _s2[0], _s2[1]
            _p2_bar, _p2_p = _s3[0], _s3[1]
            _v2_bar, _v2_p = _s4[0], _s4[1]

            if _p2_p >= _p1_p or _v2_p >= _v1_p:
                continue  # Need descending peaks AND descending valleys
            if _v2_bar < len(_mw) // 2:
                continue  # V2 must be in the recent half
            _valley_spread = (_v2_p - _v1_p) / _v1_p
            if not (-0.12 <= _valley_spread <= -0.02):
                continue  # Valleys 2-12% apart
            _rally = _p2_p - _v1_p  # Absolute rally between valleys in dollars
            _rally_pct = _rally / _v1_p
            if _rally < _atr_mw:
                continue  # Rally must be at least 1 ATR (visible on chart, not noise)

            # Trendline through P1 and P2, project to current bar
            _p_slope = (_p2_p - _p1_p) / (_p2_bar - _p1_bar)
            _proj = _p2_p + _p_slope * (len(_mw) - 1 - _p2_bar)
            if _proj <= 0:
                continue

            # Price must be at or below projected resistance, within 1.5 ATR
            # AND bouncing up from V2 (not dumping)
            _bouncing = price > _v2_p * 1.02
            if price <= _proj and price >= _proj - _atr_mw * 1.5 and _bouncing:
                # --- Proximity score (50% weight): how close to resistance trendline ---
                _dist_from_resist = (_proj - price) / _atr_mw
                _proximity_score = max(0.0, 1.0 - _dist_from_resist / 1.5)

                # --- Pattern definition score (35% weight) ---
                # Spread quality: triangular peak at 6% (geometric center of 2-12%)
                _abs_valley_spread = abs(_valley_spread)
                _spread_score = max(0.0, 1.0 - abs(_abs_valley_spread - 0.06) / 0.06)
                # Rally depth: linear ramp from 1 ATR (gate floor) to 2.5 ATR
                _rally_atr = _rally / _atr_mw
                _rally_score = min(1.0, max(0.0, (_rally_atr - 1.0) / 1.5))
                _pattern_score = 0.6 * _spread_score + 0.4 * _rally_score

                # --- Trendline validation (15% weight): intermediate touches ---
                _touch_count = 0
                for _bi in range(_p2_bar + 1, len(_mw)):
                    _tl_val = _p2_p + _p_slope * (_bi - _p2_bar)
                    if _tl_val <= 0:
                        continue
                    _bar_high = float(_mw["High"].iloc[_bi])
                    if abs(_bar_high - _tl_val) <= 0.5 * _atr_mw:
                        _touch_count += 1
                _validation_score = min(1.0, _touch_count * 0.5)

                # --- Combined quality (used for best-match selection) ---
                _raw_strength = (0.50 * _proximity_score +
                                 0.35 * _pattern_score +
                                 0.15 * _validation_score)
                _q = _raw_strength

                if not _best_w_downtrend or _q > _best_w_downtrend["_q"]:
                    _best_w_downtrend = {
                        "_q": _q,
                        "low1": round(_v1_p, 2), "low2": round(_v2_p, 2),
                        "entry_high": round(_p1_p, 2), "mid_high": round(_p2_p, 2),
                        "spread_pct": round(_abs_valley_spread * 100, 1),
                        "neckline_pct": round(_rally_pct * 100, 1),
                        "bar1": -_lookback + _v1_bar, "bar2": -_lookback + _v2_bar,
                        "price": round(price, 2), "window": _lookback,
                        "projected_resistance": round(float(_proj), 2),
                        "proximity_atr": round(_dist_from_resist, 2),
                        "proximity_score": round(_proximity_score, 2),
                        "spread_score": round(_spread_score, 2),
                        "rally_atr": round(_rally_atr, 1),
                        "rally_score": round(_rally_score, 2),
                        "pattern_score": round(_pattern_score, 2),
                        "tl_touches": _touch_count,
                        "validation_score": round(_validation_score, 2),
                    }

        # Scan for M on Downtrend: L-H-L-H with DESCENDING valleys
        #   V1(low) → P1(high) → V2(low) → P2(high), V2 < V1 (descending lows)
        #   Project trendline through V1, V2 to predict V3.
        #   Signal fires when price is near the projected V3 — PREDICTIVE entry.
        for _ci in range(len(_clean) - 3):
            _s1, _s2, _s3, _s4 = _clean[_ci], _clean[_ci + 1], _clean[_ci + 2], _clean[_ci + 3]
            if not (_s1[2] == "L" and _s2[2] == "H" and _s3[2] == "L" and _s4[2] == "H"):
                continue
            _v1_bar, _v1_p = _s1[0], _s1[1]
            _p1_bar, _p1_p = _s2[0], _s2[1]
            _v2_bar, _v2_p = _s3[0], _s3[1]
            _p2_bar, _p2_p = _s4[0], _s4[1]

            if _v2_p >= _v1_p:
                continue  # Need descending valleys (V2 < V1)
            if _p2_bar < len(_mw) // 2:
                continue  # P2 must be in recent half of window
            _valley_drop = (_v1_p - _v2_p) / _v1_p
            if not (0.02 <= _valley_drop <= 0.20):
                continue  # Valleys 2-20% apart
            _dip = _p1_p - _v2_p
            if _dip < _atr_mw:
                continue  # Dip must be at least 1 ATR

            # Trendline through V1 and V2, project V3 to current bar
            _v_slope = (_v2_p - _v1_p) / (_v2_bar - _v1_bar)
            _proj_v3 = _v2_p + _v_slope * (len(_mw) - 1 - _v2_bar)
            if _proj_v3 <= 0:
                continue

            # Shared pattern quality scores
            _spread_score = max(0.0, 1.0 - abs(_valley_drop - 0.08) / 0.08)
            _dip_atr = _dip / _atr_mw
            _dip_score = min(1.0, max(0.0, (_dip_atr - 1.0) / 1.5))
            _pattern_score = 0.6 * _spread_score + 0.4 * _dip_score

            # M on Downtrend (dip entry): price near projected V3, approaching from above
            _dist_from_v3 = price - _proj_v3
            if (-0.5 * _atr_mw <= _dist_from_v3 <= 1.5 * _atr_mw) and price < _p2_p * 0.95:
                _dist_v3_atr = abs(_dist_from_v3) / _atr_mw
                _proximity_score = max(0.0, 1.0 - _dist_v3_atr / 1.5)

                _touch_count = 0
                for _bi in range(_v2_bar + 1, len(_mw)):
                    _tl_val = _v2_p + _v_slope * (_bi - _v2_bar)
                    if _tl_val <= 0:
                        continue
                    _bar_low = float(_mw["Low"].iloc[_bi])
                    if abs(_bar_low - _tl_val) <= 0.5 * _atr_mw:
                        _touch_count += 1
                _validation_score = min(1.0, _touch_count * 0.5)

                _raw_strength = (0.50 * _proximity_score +
                                 0.35 * _pattern_score +
                                 0.15 * _validation_score)
                _q = _raw_strength

                if not _best_m_downtrend or _q > _best_m_downtrend["_q"]:
                    _best_m_downtrend = {
                        "_q": _q,
                        "v1": round(_v1_p, 2), "v2": round(_v2_p, 2),
                        "p1": round(_p1_p, 2), "p2": round(_p2_p, 2),
                        "projected_v3": round(_proj_v3, 2),
                        "bar_v1": -_lookback + _v1_bar, "bar_v2": -_lookback + _v2_bar,
                        "bar_p2": -_lookback + _p2_bar,
                        "price": round(price, 2), "window": _lookback,
                        "valley_drop_pct": round(_valley_drop * 100, 1),
                        "proximity_atr": round(_dist_v3_atr, 2),
                        "proximity_score": round(_proximity_score, 2),
                        "spread_score": round(_spread_score, 2),
                        "dip_atr": round(_dip_atr, 1),
                        "dip_score": round(_dip_score, 2),
                        "pattern_score": round(_pattern_score, 2),
                        "tl_touches": _touch_count,
                        "validation_score": round(_validation_score, 2),
                    }

            # M Breakout Down: same descending pattern, but price broke ABOVE P2
            elif price > _p2_p and price <= _p2_p * 1.10:
                _bk_pct = (price - _p2_p) / _p2_p
                _bk_proximity = max(0.0, 1.0 - _bk_pct / 0.10)
                _bk_vol_bonus = min(1.0, max(0.0, (_m_vol_ratio - 1.0) / 1.0))
                _bk_q = 0.45 * _bk_proximity + 0.25 * _bk_vol_bonus + 0.30 * _pattern_score
                if not _best_m_breakout_down or _bk_q > _best_m_breakout_down["_q"]:
                    _best_m_breakout_down = {
                        "_q": _bk_q,
                        "p2": round(_p2_p, 2),
                        "breakout_pct": round(_bk_pct * 100, 1),
                        "vol_ratio": round(_m_vol_ratio, 2),
                        "v1": round(_v1_p, 2), "v2": round(_v2_p, 2),
                        "p1": round(_p1_p, 2),
                        "price": round(price, 2), "window": _lookback,
                        "pattern_type": "descending",
                    }

    # Emit best match per pattern type
    if _best_dbl_bottom:
        _m = _best_dbl_bottom
        _db_strength = round(min(1.0, max(0.4, _m["neckline_pct"] / 100.0 / 0.10 * 0.3 + 0.4)), 2)
        signals.append({"indicator": "Double Bottom", "direction": "bullish",
                        "detail": f"Double bottom at ${_m['low1']:.2f} / ${_m['low2']:.2f}, neckline {_m['neckline_pct']:.0f}% ({_m['window']}bar)",
                        "strength": _db_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_dbl_top:
        _m = _best_dbl_top
        _dt_strength = round(min(1.0, max(0.4, abs(_m["neckline_pct"]) / 100.0 / 0.10 * 0.3 + 0.4)), 2)
        signals.append({"indicator": "Double Top", "direction": "bearish",
                        "detail": f"Double top at ${_m['high1']:.2f} / ${_m['high2']:.2f}, neckline {_m['neckline_pct']:.0f}% ({_m['window']}bar)",
                        "strength": _dt_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_m_uptrend:
        _m = _best_m_uptrend
        _m_strength = round(min(1.0, max(0.3, _m["_q"] * 0.7 + 0.3)), 2)
        signals.append({"indicator": "M on Uptrend", "direction": "bullish",
                        "detail": f"Ascending M at ${_m['high1']:.2f} / ${_m['high2']:.2f}, support ~${_m['projected_support']:.2f}, {_m['proximity_atr']:.1f} ATR away ({_m['window']}bar)",
                        "strength": _m_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_w_downtrend:
        _m = _best_w_downtrend
        _w_strength = round(min(1.0, max(0.3, _m["_q"] * 0.7 + 0.3)), 2)
        signals.append({"indicator": "W on Downtrend", "direction": "bearish",
                        "detail": f"Descending W at ${_m['low1']:.2f} / ${_m['low2']:.2f}, resistance ~${_m['projected_resistance']:.2f}, {_m['proximity_atr']:.1f} ATR away ({_m['window']}bar)",
                        "strength": _w_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_m_downtrend:
        _m = _best_m_downtrend
        _md_strength = round(min(1.0, max(0.3, _m["_q"] * 0.7 + 0.3)), 2)
        signals.append({"indicator": "M on Downtrend", "direction": "bullish",
                        "detail": f"Descending M: valleys ${_m['v1']:.2f} / ${_m['v2']:.2f}, V3 ~${_m['projected_v3']:.2f}, {_m['proximity_atr']:.1f} ATR away ({_m['window']}bar)",
                        "strength": _md_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_m_breakout_up:
        _m = _best_m_breakout_up
        _bk_strength = round(min(1.0, max(0.3, _m["_q"] * 0.7 + 0.3)), 2)
        signals.append({"indicator": "M Breakout Up", "direction": "bullish",
                        "detail": f"Ascending M broke P2 ${_m['p2']:.2f} (+{_m['breakout_pct']:.1f}%), vol {_m['vol_ratio']:.1f}x ({_m['window']}bar)",
                        "strength": _bk_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_m_breakout_down:
        _m = _best_m_breakout_down
        _bk_strength = round(min(1.0, max(0.3, _m["_q"] * 0.7 + 0.3)), 2)
        signals.append({"indicator": "M Breakout Down", "direction": "bullish",
                        "detail": f"Descending M broke P2 ${_m['p2']:.2f} (+{_m['breakout_pct']:.1f}%), vol {_m['vol_ratio']:.1f}x ({_m['window']}bar)",
                        "strength": _bk_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})

    # --- Trendline Channel (swing-point trendlines) ---
    # Detects well-defined channels by fitting lines through 3+ peaks and 3+ valleys.
    # Bullish when price bounces off support trendline; bearish at resistance trendline.
    # Uses same ATR (_atr_mw) computed for M/W detection above.
    _best_chan_support = None
    _best_chan_resist = None
    _best_chan_any = None  # best-fit channel regardless of price position (for approaching)

    for _lookback in [30, 45, 60, 80]:
        if len(df) < _lookback + 5:
            continue
        _ch = df.iloc[-_lookback:]
        _order = 3

        _ch_highs = []
        _ch_lows = []
        for _i in range(_order, len(_ch) - _order):
            _h = float(_ch["High"].iloc[_i])
            if all(_h >= float(_ch["High"].iloc[_i - _j]) for _j in range(1, _order + 1)) and \
               all(_h >= float(_ch["High"].iloc[_i + _j]) for _j in range(1, _order + 1)):
                _ch_highs.append((_i, _h))
            _l = float(_ch["Low"].iloc[_i])
            if all(_l <= float(_ch["Low"].iloc[_i - _j]) for _j in range(1, _order + 1)) and \
               all(_l <= float(_ch["Low"].iloc[_i + _j]) for _j in range(1, _order + 1)):
                _ch_lows.append((_i, _l))

        if len(_ch_highs) < 3 or len(_ch_lows) < 3:
            continue

        # Most recent swing must be in the last third of the window (channel still active)
        _last_swing_bar = max(
            max(b for b, _ in _ch_highs),
            max(b for b, _ in _ch_lows))
        if _last_swing_bar < len(_ch) * 2 // 3:
            continue

        # Fit trendlines via least-squares
        _pk_bars = np.array([b for b, _ in _ch_highs], dtype=float)
        _pk_prices = np.array([p for _, p in _ch_highs])
        _slope_p, _int_p = np.polyfit(_pk_bars, _pk_prices, 1)

        _vl_bars = np.array([b for b, _ in _ch_lows], dtype=float)
        _vl_prices = np.array([p for _, p in _ch_lows])
        _slope_v, _int_v = np.polyfit(_vl_bars, _vl_prices, 1)

        _cur_bar = len(_ch) - 1
        _proj_sup = _int_v + _slope_v * _cur_bar
        _proj_res = _int_p + _slope_p * _cur_bar
        if _proj_sup <= 0 or _proj_res <= 0:
            continue

        # Channel must be wide enough to trade (>= 1.5 ATR)
        _ch_width = _proj_res - _proj_sup
        if _ch_width < _atr_mw * 1.5:
            continue

        # Fit quality: residuals relative to ATR
        _pk_resid = [abs(p - (_int_p + _slope_p * b)) for b, p in _ch_highs]
        _vl_resid = [abs(p - (_int_v + _slope_v * b)) for b, p in _ch_lows]
        _max_pk_err = max(_pk_resid)
        _max_vl_err = max(_vl_resid)
        _avg_pk_err = sum(_pk_resid) / len(_pk_resid)
        _avg_vl_err = sum(_vl_resid) / len(_vl_resid)

        # Channel direction label
        _avg_slope = (_slope_p + _slope_v) / 2
        if _avg_slope > 0.01 * price / _lookback:
            _ch_dir = "ascending"
        elif _avg_slope < -0.01 * price / _lookback:
            _ch_dir = "descending"
        else:
            _ch_dir = "flat"

        # Track best-fit channel for approaching detection (regardless of price position)
        _fit_q = min(_avg_pk_err, _avg_vl_err) / _atr_mw if _atr_mw > 0 else 999
        if _fit_q < 1.0 and (not _best_chan_any or _fit_q < _best_chan_any.get("_fit_q", 999)):
            _best_chan_any = {
                "_fit_q": _fit_q,
                "proj_sup": round(float(_proj_sup), 2),
                "proj_res": round(float(_proj_res), 2),
                "ch_dir": _ch_dir,
                "atr": _atr_mw,
            }

        # Bullish: price near support trendline (support bounce)
        # Tight zone: 0.5 ATR below to 0.75 ATR above support — must be AT the line
        if price >= _proj_sup - _atr_mw * 0.5 and price <= _proj_sup + _atr_mw * 0.75:
            if _max_vl_err < _atr_mw * 1.5 and _avg_vl_err < _atr_mw:  # support trendline fits well
                _q = (len(_ch_lows) / 3.0) * (_ch_width / _atr_mw) * (1.0 / (1.0 + _avg_vl_err / _atr_mw))
                if not _best_chan_support or _q > _best_chan_support["_q"]:
                    _best_chan_support = {
                        "_q": _q,
                        "projected_support": round(float(_proj_sup), 2),
                        "projected_resistance": round(float(_proj_res), 2),
                        "support_slope": round(float(_slope_v), 3),
                        "resist_slope": round(float(_slope_p), 3),
                        "channel_width": round(float(_ch_width), 2),
                        "channel_width_atr": round(float(_ch_width / _atr_mw), 1),
                        "channel_dir": _ch_dir,
                        "n_peaks": len(_ch_highs),
                        "n_valleys": len(_ch_lows),
                        "avg_support_err": round(float(_avg_vl_err), 2),
                        "avg_resist_err": round(float(_avg_pk_err), 2),
                        "window": _lookback,
                        "price": round(price, 2),
                    }

        # Bearish: price near resistance trendline (resistance rejection)
        # Tight zone: 0.5 ATR above to 0.75 ATR below resistance — must be AT the line
        if price <= _proj_res + _atr_mw * 0.5 and price >= _proj_res - _atr_mw * 0.75:
            if _max_pk_err < _atr_mw * 1.5 and _avg_pk_err < _atr_mw:  # resistance trendline fits well
                _q = (len(_ch_highs) / 3.0) * (_ch_width / _atr_mw) * (1.0 / (1.0 + _avg_pk_err / _atr_mw))
                if not _best_chan_resist or _q > _best_chan_resist["_q"]:
                    _best_chan_resist = {
                        "_q": _q,
                        "projected_support": round(float(_proj_sup), 2),
                        "projected_resistance": round(float(_proj_res), 2),
                        "support_slope": round(float(_slope_v), 3),
                        "resist_slope": round(float(_slope_p), 3),
                        "channel_width": round(float(_ch_width), 2),
                        "channel_width_atr": round(float(_ch_width / _atr_mw), 1),
                        "channel_dir": _ch_dir,
                        "n_peaks": len(_ch_highs),
                        "n_valleys": len(_ch_lows),
                        "avg_support_err": round(float(_avg_pk_err), 2),
                        "avg_resist_err": round(float(_avg_pk_err), 2),
                        "window": _lookback,
                        "price": round(price, 2),
                    }

    if _best_chan_support:
        _m = _best_chan_support
        _avg_err_tc = (_m["avg_support_err"] + _m["avg_resist_err"]) / 2.0
        _tc_strength = round(min(1.0, max(0.4, min(_m["n_peaks"], _m["n_valleys"]) * 0.15 + (1.0 - min(_avg_err_tc / _atr_mw if _atr_mw > 0 else 1.0, 1.0)) * 0.2)), 2)
        signals.append({"indicator": "Trendline Channel", "direction": "bullish",
                        "detail": f"{_m['channel_dir'].title()} channel support bounce, "
                                  f"support ~${_m['projected_support']:.2f}, "
                                  f"resist ~${_m['projected_resistance']:.2f} "
                                  f"({_m['n_valleys']}V/{_m['n_peaks']}P, {_m['window']}bar)",
                        "strength": _tc_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})
    if _best_chan_resist:
        _m = _best_chan_resist
        _avg_err_tc = (_m["avg_support_err"] + _m["avg_resist_err"]) / 2.0
        _tc_strength = round(min(1.0, max(0.4, min(_m["n_peaks"], _m["n_valleys"]) * 0.15 + (1.0 - min(_avg_err_tc / _atr_mw if _atr_mw > 0 else 1.0, 1.0)) * 0.2)), 2)
        signals.append({"indicator": "Trendline Channel", "direction": "bearish",
                        "detail": f"{_m['channel_dir'].title()} channel resistance, "
                                  f"resist ~${_m['projected_resistance']:.2f}, "
                                  f"support ~${_m['projected_support']:.2f} "
                                  f"({_m['n_peaks']}P/{_m['n_valleys']}V, {_m['window']}bar)",
                        "strength": _tc_strength,
                        "meta": {k: v for k, v in _m.items() if k != "_q"}})

    # --- Trend Acceleration (weight 0.85) ---
    # MAs stacked, RSI strong but not overbought — institutions in control.
    # Volume expansion tracked but no longer gates the signal (steady trends
    # have declining volume as the move is accepted).
    if len(df) >= 55:
        ma20_ta = df["Close"].rolling(20).mean()
        ma50_ta = df["Close"].rolling(50).mean()
        if (not pd.isna(ma20_ta.iloc[-1]) and not pd.isna(ma50_ta.iloc[-1])
                and ma200_val is not None):
            rsi_ta = calculate_rsi(df, 14)
            rsi_val_ta = rsi_ta.iloc[-1] if not pd.isna(rsi_ta.iloc[-1]) else 50
            vol_recent_ta = df["Volume"].iloc[-3:].mean()
            vol_prior_ta = df["Volume"].iloc[-13:-3].mean()
            vol_expanding = vol_recent_ta > vol_prior_ta * 1.10 if vol_prior_ta > 0 else False
            vol_exp_pct = (vol_recent_ta / vol_prior_ta - 1) * 100 if vol_prior_ta > 0 else 0
            _vol_expansion_frac = vol_exp_pct / 100.0  # as a fraction for strength calc
            # Bullish: MAs stacked (20 > 50 > 200), RSI strong but not overbought
            if (ma20_ta.iloc[-1] > ma50_ta.iloc[-1] > ma200_val
                    and 55 <= rsi_val_ta <= 85):
                detail = f"MAs stacked bullish, RSI {rsi_val_ta:.0f}"
                if vol_expanding:
                    detail += ", volume expanding"
                _trend_accel_strength = round(min(1.0, max(0.5, _vol_expansion_frac / 0.3 * 0.3 + 0.4)), 2)
                signals.append({"indicator": "Trend Accel", "direction": "bullish",
                                "detail": detail,
                                "strength": _trend_accel_strength,
                                "meta": {"ma20": round(float(ma20_ta.iloc[-1]), 2),
                                         "ma50": round(float(ma50_ta.iloc[-1]), 2),
                                         "ma200": round(float(ma200_val), 2),
                                         "rsi": round(float(rsi_val_ta), 1),
                                         "vol_expansion_pct": round(float(vol_exp_pct), 1),
                                         "vol_expanding": vol_expanding,
                                         "stacking": "20>50>200"}})
            # Bearish: MAs inverted (20 < 50 < 200), RSI weak but not oversold
            elif (ma20_ta.iloc[-1] < ma50_ta.iloc[-1] < ma200_val
                    and 15 <= rsi_val_ta <= 45):
                detail = f"MAs stacked bearish, RSI {rsi_val_ta:.0f}"
                if vol_expanding:
                    detail += ", volume expanding"
                _trend_accel_strength = round(min(1.0, max(0.5, _vol_expansion_frac / 0.3 * 0.3 + 0.4)), 2)
                signals.append({"indicator": "Trend Accel", "direction": "bearish",
                                "detail": detail,
                                "strength": _trend_accel_strength,
                                "meta": {"ma20": round(float(ma20_ta.iloc[-1]), 2),
                                         "ma50": round(float(ma50_ta.iloc[-1]), 2),
                                         "ma200": round(float(ma200_val), 2),
                                         "rsi": round(float(rsi_val_ta), 1),
                                         "vol_expansion_pct": round(float(vol_exp_pct), 1),
                                         "vol_expanding": vol_expanding,
                                         "stacking": "20<50<200"}})

    # --- Mean Reversion Extreme (weight 0.75) ---
    # Price stretched 2+ standard deviations from 50 MA — rubber band snapback
    if len(df) >= 55:
        ma50_mr = df["Close"].rolling(50).mean()
        std50_mr = df["Close"].rolling(50).std()
        if not pd.isna(ma50_mr.iloc[-1]) and not pd.isna(std50_mr.iloc[-1]) and std50_mr.iloc[-1] > 0:
            z_score = (price - ma50_mr.iloc[-1]) / std50_mr.iloc[-1]
            dist_pct = (price - float(ma50_mr.iloc[-1])) / float(ma50_mr.iloc[-1]) * 100
            if z_score < -2.0:
                _mr_strength = round(min(1.0, max(0.3, (abs(float(z_score)) - 1.5) / 2.0)), 2)
                signals.append({"indicator": "Mean Reversion", "direction": "bullish",
                                "detail": f"Extended {z_score:.1f} std devs below 50 MA (${ma50_mr.iloc[-1]:.2f})",
                                "strength": _mr_strength,
                                "meta": {"z_score": round(float(z_score), 2),
                                         "ma50_val": round(float(ma50_mr.iloc[-1]), 2),
                                         "std_val": round(float(std50_mr.iloc[-1]), 2),
                                         "price": round(price, 2),
                                         "distance_pct": round(dist_pct, 1)}})
            elif z_score > 2.0:
                _mr_strength = round(min(1.0, max(0.3, (abs(float(z_score)) - 1.5) / 2.0)), 2)
                signals.append({"indicator": "Mean Reversion", "direction": "bearish",
                                "detail": f"Extended +{z_score:.1f} std devs above 50 MA (${ma50_mr.iloc[-1]:.2f})",
                                "strength": _mr_strength,
                                "meta": {"z_score": round(float(z_score), 2),
                                         "ma50_val": round(float(ma50_mr.iloc[-1]), 2),
                                         "std_val": round(float(std50_mr.iloc[-1]), 2),
                                         "price": round(price, 2),
                                         "distance_pct": round(dist_pct, 1)}})

    # --- Accumulation / Distribution (weight 0.85) ---
    # Detect institutional buying/selling while price bases — persists for weeks,
    # won't be missed between manual scans like momentum signals can be
    if len(df) >= 25:
        lookback = df.iloc[-20:]

        # 1. OBV divergence: volume flowing in/out while price stays flat
        obv = [0.0]
        for i in range(1, len(lookback)):
            if lookback["Close"].iloc[i] > lookback["Close"].iloc[i - 1]:
                obv.append(obv[-1] + lookback["Volume"].iloc[i])
            elif lookback["Close"].iloc[i] < lookback["Close"].iloc[i - 1]:
                obv.append(obv[-1] - lookback["Volume"].iloc[i])
            else:
                obv.append(obv[-1])
        obv_slope = obv[-1] - obv[0]
        price_change = (lookback["Close"].iloc[-1] - lookback["Close"].iloc[0]) / lookback["Close"].iloc[0]
        obv_bullish = obv_slope > 0 and price_change < 0.02
        obv_bearish = obv_slope < 0 and price_change > -0.02

        # 2. Up/down volume ratio: who's buying more aggressively?
        up_vol = lookback.loc[lookback["Close"] > lookback["Close"].shift(1), "Volume"].sum()
        dn_vol = lookback.loc[lookback["Close"] < lookback["Close"].shift(1), "Volume"].sum()
        vol_ratio_ud = up_vol / dn_vol if dn_vol > 0 else 2.0
        accum_vol = vol_ratio_ud > 1.3
        distrib_vol = vol_ratio_ud < distrib_threshold

        # 3. Price basing: tight range = supply being absorbed
        range_pct = (lookback["High"].max() - lookback["Low"].min()) / lookback["Low"].min()
        basing = range_pct < 0.15

        # Bullish accumulation: strong (2+ conditions, weight 0.85) vs
        # early (1 condition, weight 0.5) — tracked separately so the
        # adaptive engine can learn if early signals have edge or are noise.
        _accum_meta = {"lookback": 20, "vol_ratio": round(float(vol_ratio_ud), 2),
                       "obv_slope": round(float(obv_slope)),
                       "price_chg_pct": round(float(price_change) * 100, 2),
                       "range_pct": round(float(range_pct) * 100, 1),
                       "basing": bool(basing)}

        bull_conditions = sum([obv_bullish, accum_vol, basing])
        if bull_conditions >= 2:
            signals.append({
                "indicator": "Accumulation", "direction": "bullish",
                "detail": f"Institutional accumulation — {vol_ratio_ud:.1f}x buy/sell vol, "
                          f"{'OBV diverging up, ' if obv_bullish else ''}"
                          f"{'tight base' if basing else 'building'}"
                          f" ({bull_conditions}/3 conditions)",
                "strength": 0.85,
                "meta": {**_accum_meta, "obv_rising": bool(obv_bullish),
                         "accum_vol": bool(accum_vol), "conditions_met": int(bull_conditions)}
            })
        elif bull_conditions == 1:
            signals.append({
                "indicator": "Early Accumulation", "direction": "bullish",
                "detail": f"Early accumulation — {vol_ratio_ud:.1f}x buy/sell vol, "
                          f"{'OBV diverging up, ' if obv_bullish else ''}"
                          f"{'tight base' if basing else 'building'}"
                          f" (1/3 conditions)",
                "strength": 0.4,
                "meta": {**_accum_meta, "obv_rising": bool(obv_bullish),
                         "accum_vol": bool(accum_vol), "conditions_met": 1}
            })

        # Bearish distribution: same split, distinct indicator names
        bear_conditions = sum([obv_bearish, distrib_vol, basing])
        if bear_conditions >= 2:
            signals.append({
                "indicator": "Distribution", "direction": "bearish",
                "detail": f"Institutional distribution — {vol_ratio_ud:.1f}x buy/sell vol, "
                          f"{'OBV diverging down, ' if obv_bearish else ''}"
                          f"{'tight range' if basing else 'weakening'}"
                          f" ({bear_conditions}/3 conditions)",
                "strength": 0.85,
                "meta": {**_accum_meta, "obv_falling": bool(obv_bearish),
                         "distrib_vol": bool(distrib_vol), "conditions_met": int(bear_conditions)}
            })
        elif bear_conditions == 1:
            signals.append({
                "indicator": "Early Distribution", "direction": "bearish",
                "detail": f"Early distribution — {vol_ratio_ud:.1f}x buy/sell vol, "
                          f"{'OBV diverging down, ' if obv_bearish else ''}"
                          f"{'tight range' if basing else 'weakening'}"
                          f" (1/3 conditions)",
                "strength": 0.4,
                "meta": {**_accum_meta, "obv_falling": bool(obv_bearish),
                         "distrib_vol": bool(distrib_vol), "conditions_met": 1}
            })

    # --- Range Compression (weight 0.85) ---
    # ATR(20) at 6-month low + descending highs + ascending lows = coiled spring.
    # A breakout is imminent — direction unknown, but the move will be big.
    # This persists for days/weeks, perfect for manual scan timing.
    if len(df) >= 130:
        atr20 = calculate_atr(df, period=20)
        current_atr = atr20.iloc[-1]
        # ATR at 6-month (~130 trading day) low
        atr_6mo = atr20.iloc[-130:]
        atr_6mo_valid = atr_6mo.dropna()
        if len(atr_6mo_valid) >= 20 and not pd.isna(current_atr) and current_atr > 0:
            atr_min = atr_6mo_valid.min()
            atr_near_low = current_atr <= atr_min * 1.10  # within 10% of 6-month low

            # Descending highs over last 20 bars
            highs_20 = df["High"].iloc[-20:]
            first_half_high = highs_20.iloc[:10].max()
            second_half_high = highs_20.iloc[10:].max()
            highs_descending = second_half_high < first_half_high

            # Ascending lows over last 20 bars
            lows_20 = df["Low"].iloc[-20:]
            first_half_low = lows_20.iloc[:10].min()
            second_half_low = lows_20.iloc[10:].min()
            lows_ascending = second_half_low > first_half_low

            if atr_near_low and highs_descending and lows_ascending:
                compression_pct = (second_half_high - second_half_low) / second_half_low * 100
                _rc_meta = {"current_atr": round(float(current_atr), 2),
                            "atr_min": round(float(atr_min), 2),
                            "compression_pct": round(float(compression_pct), 1),
                            "highs_desc": bool(highs_descending),
                            "lows_asc": bool(lows_ascending)}
                # Direction hint: if price is above key MAs, lean bullish
                if ma200_val and price > ma200_val:
                    signals.append({
                        "indicator": "Range Compression", "direction": "bullish",
                        "detail": f"Coiled spring — ATR near 6mo low, range {compression_pct:.1f}%, above 200 MA",
                        "strength": 0.7,
                        "meta": _rc_meta
                    })
                elif ma200_val and price < ma200_val:
                    signals.append({
                        "indicator": "Range Compression", "direction": "bearish",
                        "detail": f"Coiled spring — ATR near 6mo low, range {compression_pct:.1f}%, below 200 MA",
                        "strength": 0.7,
                        "meta": _rc_meta
                    })
                else:
                    # No 200 MA data — default bullish (breakouts favor upside)
                    signals.append({
                        "indicator": "Range Compression", "direction": "bullish",
                        "detail": f"Coiled spring — ATR near 6mo low, range {compression_pct:.1f}%",
                        "strength": 0.7,
                        "meta": _rc_meta
                    })

    # --- Dist Breakdown (weight 0.85) ---
    # Lower highs forming + distribution volume = structural bearish.
    # Requires: 3+ swing highs each lower, selling volume > buying volume,
    # price below 50 EMA or near resistance.
    if len(df) >= 60:
        # Find swing highs: bar whose High is the max in a 5-bar window
        highs = df["High"].values
        swing_highs = []  # list of (index, price)
        for si in range(2, len(highs) - 2):
            if highs[si] == max(highs[si - 2 : si + 3]):
                # Avoid duplicates within 5 bars of each other
                if not swing_highs or si - swing_highs[-1][0] >= 5:
                    swing_highs.append((si, float(highs[si])))

        # Check last 60 bars for 3+ descending swing highs
        cutoff_idx = len(highs) - 60
        recent_swings = [(i, p) for i, p in swing_highs if i >= cutoff_idx]

        if len(recent_swings) >= 3:
            # Check if at least 3 consecutive swing highs are descending
            descending_count = 1
            max_descending = 1
            for si_idx in range(1, len(recent_swings)):
                if recent_swings[si_idx][1] < recent_swings[si_idx - 1][1]:
                    descending_count += 1
                    max_descending = max(max_descending, descending_count)
                else:
                    descending_count = 1

            if max_descending >= 3:
                # Distribution volume: more volume on down candles than up candles (last 20 bars)
                last_20 = df.iloc[-20:]
                down_vol = last_20.loc[last_20["Close"] < last_20["Open"], "Volume"].sum()
                up_vol = last_20.loc[last_20["Close"] >= last_20["Open"], "Volume"].sum()
                has_distribution = down_vol > up_vol * 0.85

                # Price below 50 EMA or near resistance
                ema50 = df["Close"].ewm(span=50, adjust=False).mean()
                below_ema50 = price < ema50.iloc[-1]

                # "Near resistance" = price within 2% of the most recent swing high
                last_swing_price = recent_swings[-1][1]
                near_resist = price >= last_swing_price * 0.98 and price <= last_swing_price * 1.01

                if has_distribution and (below_ema50 or near_resist):
                    dist_ratio = down_vol / up_vol if up_vol > 0 else 99
                    _db_str = round(min(1.0, max(0.5, float(dist_ratio) / 1.5 * 0.5 + 0.3)), 2)
                    signals.append({
                        "indicator": "Dist Breakdown",
                        "direction": "bearish",
                        "detail": (f"{max_descending} lower highs, "
                                   f"sell vol {dist_ratio:.1f}x buy vol"
                                   f"{', below 50 EMA' if below_ema50 else ', near resistance'}"),
                        "strength": _db_str,
                        "meta": {"n_lower_highs": int(max_descending),
                                 "dist_ratio": round(float(dist_ratio), 1),
                                 "below_ema50": bool(below_ema50),
                                 "near_resist": bool(near_resist)}
                    })

                # --- Descending Trendline Breakout (weight 1.0) ---
                # Same lower highs, but price broke ABOVE the descending resistance.
                # Classic M-on-downtrend breakout through the line connecting peaks.
                # Re-find the longest descending run to extract actual peak positions.
                _run_len = 1
                _best_end = 0
                for _ri in range(1, len(recent_swings)):
                    if recent_swings[_ri][1] < recent_swings[_ri - 1][1]:
                        _run_len += 1
                        if _run_len >= max_descending:
                            _best_end = _ri
                    else:
                        _run_len = 1
                _best_start = _best_end - max_descending + 1
                desc_peaks = recent_swings[_best_start:_best_end + 1]

                if len(desc_peaks) >= 3:
                    _pk_bars = [p[0] for p in desc_peaks]
                    _pk_prices = [p[1] for p in desc_peaks]
                    _tl_slope, _tl_int = np.polyfit(_pk_bars, _pk_prices, 1)
                    _cur_bar = len(df) - 1

                    # Last peak must be recent (within 30 bars) — stale projections diverge
                    if _tl_slope < 0 and _cur_bar - _pk_bars[-1] <= 30:
                        _proj_res = _tl_int + _tl_slope * _cur_bar
                        _atr_tb = float(calculate_atr(df, 14).iloc[-1]) if not pd.isna(calculate_atr(df, 14).iloc[-1]) else 0

                        # Price broke above projected resistance (0.25 ATR buffer)
                        if _atr_tb > 0 and price > _proj_res + _atr_tb * 0.25:
                            _avg_vol_tb = df["Volume"].iloc[-20:].mean()
                            _cur_vol_tb = df["Volume"].iloc[-1]
                            _vol_ratio = _cur_vol_tb / _avg_vol_tb if _avg_vol_tb > 0 else 0

                            if _vol_ratio >= 1.2:
                                _tlb_strength = round(min(1.0, max(0.5, float(_vol_ratio) / 1.5 * 0.3 + 0.4)), 2)
                                signals.append({
                                    "indicator": "Trendline Break",
                                    "direction": "bullish",
                                    "detail": (f"Broke above descending resistance "
                                               f"(${_proj_res:.2f}, {len(desc_peaks)} lower highs, "
                                               f"{_vol_ratio:.1f}x vol)"),
                                    "strength": _tlb_strength,
                                    "meta": {"proj_resistance": round(float(_proj_res), 2),
                                             "slope": round(float(_tl_slope), 4),
                                             "n_peaks": len(desc_peaks),
                                             "vol_ratio": round(float(_vol_ratio), 1),
                                             "price": round(price, 2)}
                                })

    # --- Higher Lows Breakout (weight 0.85) ---
    # Bullish mirror of Dist Breakdown: ascending swing lows + accumulation volume.
    # Requires: 3+ swing lows each higher, buying volume > selling volume,
    # price above 50 EMA or near support.
    if len(df) >= 60:
        lows = df["Low"].values
        swing_lows = []
        for si in range(2, len(lows) - 2):
            if lows[si] == min(lows[si - 2 : si + 3]):
                if not swing_lows or si - swing_lows[-1][0] >= 5:
                    swing_lows.append((si, float(lows[si])))

        cutoff_idx = len(lows) - 60
        recent_lows = [(i, p) for i, p in swing_lows if i >= cutoff_idx]

        if len(recent_lows) >= 3:
            ascending_count = 1
            max_ascending = 1
            for si_idx in range(1, len(recent_lows)):
                if recent_lows[si_idx][1] > recent_lows[si_idx - 1][1]:
                    ascending_count += 1
                    max_ascending = max(max_ascending, ascending_count)
                else:
                    ascending_count = 1

            if max_ascending >= 3:
                last_20 = df.iloc[-20:]
                up_vol_hl = last_20.loc[last_20["Close"] >= last_20["Open"], "Volume"].sum()
                down_vol_hl = last_20.loc[last_20["Close"] < last_20["Open"], "Volume"].sum()
                has_accumulation = up_vol_hl > down_vol_hl * 0.85

                ema50 = df["Close"].ewm(span=50, adjust=False).mean()
                above_ema50 = price > ema50.iloc[-1]

                last_low_price = recent_lows[-1][1]
                near_support = price <= last_low_price * 1.02 and price >= last_low_price * 0.99

                if has_accumulation and (above_ema50 or near_support):
                    accum_ratio = up_vol_hl / down_vol_hl if down_vol_hl > 0 else 99
                    _hl_str = round(min(1.0, max(0.5, float(accum_ratio) / 1.5 * 0.5 + 0.3)), 2)
                    signals.append({
                        "indicator": "Higher Lows",
                        "direction": "bullish",
                        "detail": (f"{max_ascending} higher lows, "
                                   f"buy vol {accum_ratio:.1f}x sell vol"
                                   f"{', above 50 EMA' if above_ema50 else ', near support'}"),
                        "strength": _hl_str,
                        "meta": {"n_higher_lows": int(max_ascending),
                                 "accum_ratio": round(float(accum_ratio), 1),
                                 "above_ema50": bool(above_ema50),
                                 "near_support": bool(near_support)}
                    })

                # --- Ascending Trendline Breakdown (weight 1.0) ---
                # Higher lows formed ascending support; price broke BELOW it.
                # Bearish mirror of Descending Trendline Breakout.
                _run_len_asc = 1
                _best_end_asc = 0
                for _ri in range(1, len(recent_lows)):
                    if recent_lows[_ri][1] > recent_lows[_ri - 1][1]:
                        _run_len_asc += 1
                        if _run_len_asc >= max_ascending:
                            _best_end_asc = _ri
                    else:
                        _run_len_asc = 1
                _best_start_asc = _best_end_asc - max_ascending + 1
                asc_valleys = recent_lows[_best_start_asc:_best_end_asc + 1]

                if len(asc_valleys) >= 3:
                    _vl_bars = [p[0] for p in asc_valleys]
                    _vl_prices = [p[1] for p in asc_valleys]
                    _tl_slope_a, _tl_int_a = np.polyfit(_vl_bars, _vl_prices, 1)
                    _cur_bar_a = len(df) - 1

                    # Last valley must be recent (within 30 bars) — stale projections diverge
                    if _tl_slope_a > 0 and _cur_bar_a - _vl_bars[-1] <= 30:
                        _proj_sup = _tl_int_a + _tl_slope_a * _cur_bar_a
                        _atr_tb_a = float(calculate_atr(df, 14).iloc[-1]) if not pd.isna(calculate_atr(df, 14).iloc[-1]) else 0

                        # Price broke below projected support (0.25 ATR buffer)
                        if _atr_tb_a > 0 and price < _proj_sup - _atr_tb_a * 0.25:
                            _avg_vol_tb_a = df["Volume"].iloc[-20:].mean()
                            _cur_vol_tb_a = df["Volume"].iloc[-1]
                            _vol_ratio_a = _cur_vol_tb_a / _avg_vol_tb_a if _avg_vol_tb_a > 0 else 0

                            if _vol_ratio_a >= 1.2:
                                _tlb_strength_a = round(min(1.0, max(0.5, float(_vol_ratio_a) / 1.5 * 0.3 + 0.4)), 2)
                                signals.append({
                                    "indicator": "Trendline Break",
                                    "direction": "bearish",
                                    "detail": (f"Broke below ascending support "
                                               f"(${_proj_sup:.2f}, {len(asc_valleys)} higher lows, "
                                               f"{_vol_ratio_a:.1f}x vol)"),
                                    "strength": _tlb_strength_a,
                                    "meta": {"proj_support": round(float(_proj_sup), 2),
                                             "slope": round(float(_tl_slope_a), 4),
                                             "n_valleys": len(asc_valleys),
                                             "vol_ratio": round(float(_vol_ratio_a), 1),
                                             "price": round(price, 2)}
                                })

    # --- Deep Pullback — price extended below key MAs (weight 0.85) ---
    # Detects stocks trading significantly below 200 EMA, 21 EMA, and/or 5 EMA.
    # These are potential dip-buy setups: the "calm before the storm" where price
    # has pulled back hard and could snap back to the MAs.
    # Requires at least 2 of 3 MAs to confirm it's a real dip, not just one MA.
    # Only fires bullish — the thesis is buying the dip, not shorting the rip.
    if len(df) >= 21:
        _dp_ema200 = ma200_val  # Already calculated at top of function
        _dp_ema21 = float(ema21.iloc[-1]) if len(ema21) >= 1 and not pd.isna(ema21.iloc[-1]) else None
        _dp_ema5 = calculate_ema(df, 5)
        _dp_ema5_val = float(_dp_ema5.iloc[-1]) if len(_dp_ema5) >= 1 and not pd.isna(_dp_ema5.iloc[-1]) else None

        _dips = []
        _dip_meta = {}

        if _dp_ema200 is not None and price < _dp_ema200:
            _dist_200 = (price - _dp_ema200) / _dp_ema200 * 100
            if _dist_200 <= -2.0:  # 2%+ below 200 EMA
                _dips.append(("200 EMA", _dp_ema200, _dist_200))
                _dip_meta["ema200"] = round(_dp_ema200, 2)
                _dip_meta["dist_200"] = round(_dist_200, 2)

        if _dp_ema21 is not None and price < _dp_ema21:
            _dist_21 = (price - _dp_ema21) / _dp_ema21 * 100
            if _dist_21 <= -3.0:  # 3%+ below 21 EMA
                _dips.append(("21 EMA", _dp_ema21, _dist_21))
                _dip_meta["ema21"] = round(_dp_ema21, 2)
                _dip_meta["dist_21"] = round(_dist_21, 2)

        if _dp_ema5_val is not None and price < _dp_ema5_val:
            _dist_5 = (price - _dp_ema5_val) / _dp_ema5_val * 100
            if _dist_5 <= -2.0:  # 2%+ below 5 EMA
                _dips.append(("5 EMA", _dp_ema5_val, _dist_5))
                _dip_meta["ema5"] = round(_dp_ema5_val, 2)
                _dip_meta["dist_5"] = round(_dist_5, 2)

        # Need at least 2 MAs confirming the dip
        if len(_dips) >= 2:
            # Sort by deepest dip first
            _dips.sort(key=lambda x: x[2])
            _detail_parts = [f"{d[2]:+.1f}% below {d[0]}" for d in _dips]
            _dp_strength = round(min(1.0, max(0.4, len(_dips) * 0.2 + 0.2)), 2)
            signals.append({
                "indicator": "Deep Pullback",
                "direction": "bullish",
                "detail": f"Extended dip: {', '.join(_detail_parts)}",
                "strength": _dp_strength,
                "meta": {
                    "price": round(price, 2),
                    "n_mas_below": len(_dips),
                    "deepest_pct": round(_dips[0][2], 2),
                    **_dip_meta,
                }
            })

    # --- Approaching setup detection ---
    _ema21_val = float(ema21.iloc[-1]) if len(ema21) > 0 and not pd.isna(ema21.iloc[-1]) else None
    approaching = detect_approaching_setups(
        price, _ema21_val, ma200_val, support, resistance,
        _best_chan_any, _partial_dbl_bottom,
        _atr_val, _atr_pct, ticker=ticker,
    )

    # --- Raw metrics for leaderboard (proximity data not captured in signals) ---
    metrics = {
        "ma200_val": round(float(ma200_val), 2) if ma200_val is not None else None,
        "support": round(float(support), 2) if support > 0 else None,
        "resistance": round(float(resistance), 2) if resistance > 0 else None,
        "ema21_val": round(float(_ema21_val), 2) if _ema21_val is not None else None,
        "rsi": _rsi_current,
    }

    return signals, trend_200, vcp, recent_earnings, earnings_dates, approaching, metrics, eps_data, rev_data
