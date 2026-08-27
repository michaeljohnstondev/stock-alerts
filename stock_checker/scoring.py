import math

import pandas as pd

from .constants import (
    SIGNAL_WEIGHTS, MAX_INDICATOR_SCORE, MAX_TREND_SCORE,
    MAX_VOLUME_SCORE, MAX_CONVICTION_SCORE, PILLAR_DEFAULTS,
    TIER_A_MIN, TIER_B_MIN, TIER_C_MIN,
    TREND_GATE_MIN, TREND_GATE_CAP,
    EV_SIZE_MIN, EV_SIZE_WIN_MIN,
    COUNTER_TREND_SIGNALS, WITH_TREND_SIGNALS, TREND_DISCOUNT_COUNTER,
    ADVERSE_STACK_PENALTY,
    CHOP_TREND_DISCOUNT, CHOP_TREND_GATE_MIN,
    HIGH_52W_OVERBOUGHT_WR_PENALTY,
    DIVERGENCE_MKT_WR_PENALTY,
    VALUE_GRADES,
    SIGNAL_TO_FACTOR, FACTOR_WEIGHTS,
)
from .market import market_agrees
from .adaptive import (
    _get_adaptive_rs_adjustment, _get_adaptive_sector_adjustment,
    get_sigmoid_params,
)


# ---------------------------------------------------------------------------
# Value scoring — fundamentals as a separate lens
# ---------------------------------------------------------------------------

def _value_sub_valuation(fund: dict, direction: str) -> tuple:
    """Score valuation (0-25). Longs: cheap = high. Shorts: expensive = high."""
    fwd_pe = fund.get("forward_pe")
    peg = fund.get("peg_ratio")
    parts = []

    if fwd_pe is None and peg is None:
        return 12, "no data"

    score = 0
    n = 0

    if fwd_pe is not None and fwd_pe > 0:
        n += 1
        if direction == "bullish":
            if fwd_pe < 12:
                score += 25
                parts.append(f"Fwd P/E {fwd_pe:.1f} (cheap)")
            elif fwd_pe < 18:
                score += 20
                parts.append(f"Fwd P/E {fwd_pe:.1f} (reasonable)")
            elif fwd_pe < 25:
                score += 12
                parts.append(f"Fwd P/E {fwd_pe:.1f} (fair)")
            elif fwd_pe < 40:
                score += 6
                parts.append(f"Fwd P/E {fwd_pe:.1f} (pricey)")
            else:
                score += 2
                parts.append(f"Fwd P/E {fwd_pe:.1f} (expensive)")
        else:  # bearish — expensive is good for shorts
            if fwd_pe > 40:
                score += 25
                parts.append(f"Fwd P/E {fwd_pe:.1f} (stretched)")
            elif fwd_pe > 25:
                score += 18
                parts.append(f"Fwd P/E {fwd_pe:.1f} (rich)")
            elif fwd_pe > 18:
                score += 10
                parts.append(f"Fwd P/E {fwd_pe:.1f} (fair)")
            else:
                score += 4
                parts.append(f"Fwd P/E {fwd_pe:.1f} (cheap)")
    elif fwd_pe is not None and fwd_pe <= 0:
        # Negative PE = no earnings
        if direction == "bearish":
            n += 1
            score += 22
            parts.append("no earnings (bearish)")
        else:
            n += 1
            score += 3
            parts.append("no earnings")

    if peg is not None and peg > 0:
        n += 1
        if direction == "bullish":
            if peg < 1.0:
                score += 25
                parts.append(f"PEG {peg:.2f} (growth bargain)")
            elif peg < 1.5:
                score += 18
                parts.append(f"PEG {peg:.2f} (fair)")
            elif peg < 2.5:
                score += 10
                parts.append(f"PEG {peg:.2f} (pricey)")
            else:
                score += 4
                parts.append(f"PEG {peg:.2f} (expensive)")
        else:
            if peg > 2.5:
                score += 22
                parts.append(f"PEG {peg:.2f} (overpriced)")
            elif peg > 1.5:
                score += 14
                parts.append(f"PEG {peg:.2f} (rich)")
            else:
                score += 6
                parts.append(f"PEG {peg:.2f} (reasonable)")

    avg = score / n if n > 0 else 12
    return min(25, round(avg)), ", ".join(parts)


def _value_sub_profitability(fund: dict, direction: str) -> tuple:
    """Score profitability (0-25). Longs: strong margins = high."""
    margins = fund.get("profit_margins")
    roe = fund.get("return_on_equity")
    parts = []

    if margins is None and roe is None:
        return 12, "no data"

    score = 0
    n = 0

    if margins is not None:
        n += 1
        m_pct = margins * 100
        if direction == "bullish":
            if m_pct > 20:
                score += 25
                parts.append(f"margins {m_pct:.1f}% (strong)")
            elif m_pct > 10:
                score += 18
                parts.append(f"margins {m_pct:.1f}% (healthy)")
            elif m_pct > 0:
                score += 10
                parts.append(f"margins {m_pct:.1f}% (thin)")
            else:
                score += 3
                parts.append(f"margins {m_pct:.1f}% (negative)")
        else:
            if m_pct < 0:
                score += 25
                parts.append(f"margins {m_pct:.1f}% (losing money)")
            elif m_pct < 5:
                score += 18
                parts.append(f"margins {m_pct:.1f}% (razor thin)")
            elif m_pct < 15:
                score += 10
                parts.append(f"margins {m_pct:.1f}% (moderate)")
            else:
                score += 5
                parts.append(f"margins {m_pct:.1f}% (strong)")

    if roe is not None:
        n += 1
        r_pct = roe * 100
        if direction == "bullish":
            if r_pct > 20:
                score += 25
                parts.append(f"ROE {r_pct:.1f}% (excellent)")
            elif r_pct > 12:
                score += 18
                parts.append(f"ROE {r_pct:.1f}% (good)")
            elif r_pct > 0:
                score += 10
                parts.append(f"ROE {r_pct:.1f}% (low)")
            else:
                score += 3
                parts.append(f"ROE {r_pct:.1f}% (negative)")
        else:
            if r_pct < 0:
                score += 25
                parts.append(f"ROE {r_pct:.1f}% (negative)")
            elif r_pct < 8:
                score += 18
                parts.append(f"ROE {r_pct:.1f}% (weak)")
            elif r_pct < 15:
                score += 10
                parts.append(f"ROE {r_pct:.1f}% (moderate)")
            else:
                score += 5
                parts.append(f"ROE {r_pct:.1f}% (strong)")

    avg = score / n if n > 0 else 12
    return min(25, round(avg)), ", ".join(parts)


def _value_sub_health(fund: dict, direction: str) -> tuple:
    """Score financial health (0-25). Longs: low debt + cash = high."""
    de = fund.get("debt_to_equity")
    fcf = fund.get("free_cashflow")
    parts = []

    if de is None and fcf is None:
        return 12, "no data"

    score = 0
    n = 0

    if de is not None:
        n += 1
        if direction == "bullish":
            if de < 30:
                score += 25
                parts.append(f"D/E {de:.0f}% (fortress)")
            elif de < 80:
                score += 18
                parts.append(f"D/E {de:.0f}% (healthy)")
            elif de < 150:
                score += 10
                parts.append(f"D/E {de:.0f}% (moderate)")
            else:
                score += 4
                parts.append(f"D/E {de:.0f}% (heavy)")
        else:
            if de > 200:
                score += 25
                parts.append(f"D/E {de:.0f}% (debt-laden)")
            elif de > 100:
                score += 18
                parts.append(f"D/E {de:.0f}% (leveraged)")
            elif de > 50:
                score += 10
                parts.append(f"D/E {de:.0f}% (moderate)")
            else:
                score += 5
                parts.append(f"D/E {de:.0f}% (low)")

    if fcf is not None:
        n += 1
        if direction == "bullish":
            if fcf > 0:
                score += 22
                parts.append(f"FCF positive")
            else:
                score += 5
                parts.append(f"FCF negative")
        else:
            if fcf < 0:
                score += 22
                parts.append(f"FCF negative (burning cash)")
            else:
                score += 8
                parts.append(f"FCF positive")

    avg = score / n if n > 0 else 12
    return min(25, round(avg)), ", ".join(parts)


def _value_sub_growth(fund: dict, direction: str) -> tuple:
    """Score growth (0-25). Longs: growing = high."""
    rev_g = fund.get("revenue_growth")
    earn_g = fund.get("earnings_growth")
    parts = []

    if rev_g is None and earn_g is None:
        return 12, "no data"

    score = 0
    n = 0

    if rev_g is not None:
        n += 1
        r_pct = rev_g * 100
        if direction == "bullish":
            if r_pct > 20:
                score += 25
                parts.append(f"rev {r_pct:+.1f}% (strong)")
            elif r_pct > 8:
                score += 18
                parts.append(f"rev {r_pct:+.1f}% (solid)")
            elif r_pct > 0:
                score += 12
                parts.append(f"rev {r_pct:+.1f}% (modest)")
            else:
                score += 4
                parts.append(f"rev {r_pct:+.1f}% (shrinking)")
        else:
            if r_pct < -10:
                score += 25
                parts.append(f"rev {r_pct:+.1f}% (declining)")
            elif r_pct < 0:
                score += 18
                parts.append(f"rev {r_pct:+.1f}% (shrinking)")
            elif r_pct < 5:
                score += 10
                parts.append(f"rev {r_pct:+.1f}% (stagnant)")
            else:
                score += 5
                parts.append(f"rev {r_pct:+.1f}% (growing)")

    if earn_g is not None:
        n += 1
        e_pct = earn_g * 100
        if direction == "bullish":
            if e_pct > 25:
                score += 25
                parts.append(f"EPS {e_pct:+.1f}% (accelerating)")
            elif e_pct > 10:
                score += 18
                parts.append(f"EPS {e_pct:+.1f}% (growing)")
            elif e_pct > 0:
                score += 12
                parts.append(f"EPS {e_pct:+.1f}% (flat)")
            else:
                score += 4
                parts.append(f"EPS {e_pct:+.1f}% (declining)")
        else:
            if e_pct < -15:
                score += 25
                parts.append(f"EPS {e_pct:+.1f}% (collapsing)")
            elif e_pct < 0:
                score += 18
                parts.append(f"EPS {e_pct:+.1f}% (shrinking)")
            elif e_pct < 5:
                score += 10
                parts.append(f"EPS {e_pct:+.1f}% (stagnant)")
            else:
                score += 5
                parts.append(f"EPS {e_pct:+.1f}% (growing)")

    avg = score / n if n > 0 else 12
    return min(25, round(avg)), ", ".join(parts)


def score_value(fundamentals: dict, direction: str) -> dict:
    """Score fundamental quality on a 0-100 scale, returns grade + breakdown.
    Direction-aware: longs reward good fundamentals, shorts reward bad.
    Missing data = neutral (12/25 per sub-score) — never penalizes."""
    if not fundamentals:
        return {"grade": "N/A", "score": None, "valuation": None,
                "profitability": None, "health": None, "growth": None,
                "details": {}}

    val_score, val_detail = _value_sub_valuation(fundamentals, direction)
    prof_score, prof_detail = _value_sub_profitability(fundamentals, direction)
    health_score, health_detail = _value_sub_health(fundamentals, direction)
    growth_score, growth_detail = _value_sub_growth(fundamentals, direction)

    total = val_score + prof_score + health_score + growth_score

    grade = "F"
    for threshold, letter in VALUE_GRADES:
        if total >= threshold:
            grade = letter
            break

    return {
        "grade": grade,
        "score": total,
        "valuation": val_score,
        "profitability": prof_score,
        "health": health_score,
        "growth": growth_score,
        "details": {
            "valuation": val_detail,
            "profitability": prof_detail,
            "health": health_detail,
            "growth": growth_detail,
        },
    }


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def score_volume(vol_ratio: float) -> float:
    """Score volume on a 0-2 scale."""
    if vol_ratio < 0.8:
        return 0.0
    elif vol_ratio < 1.0:
        return 0.25
    elif vol_ratio < 1.5:
        return 0.5 + (vol_ratio - 1.0) * 1.0  # 0.5 to 1.0
    elif vol_ratio < 2.0:
        return 1.0 + (vol_ratio - 1.5) * 1.0  # 1.0 to 1.5
    elif vol_ratio < 3.0:
        return 1.5 + (vol_ratio - 2.0) * 0.5  # 1.5 to 2.0
    else:
        return 2.0


def check_trend_strength(signals: list, direction: str, trend_200: str,
                         htf_trend: str, market_trends: dict,
                         df: pd.DataFrame = None,
                         vol_ratio: float = 1.0,
                         rs_raw: float = None) -> int:
    """Count how many trend strength conditions are met.
    For LONGS, require demand confirmation — not just an oversold bounce.
    For SHORTS, require supply confirmation — not just an overbought reading.
    Conditions 1-5 are trend/structure checks.
    Conditions 6-8 are "backdoors" — non-trend confirmations that prove
    institutional commitment even when the trend hasn't turned yet.
    Returns count of conditions met (0-8)."""
    conditions_met = 0
    indicator_names = [s["indicator"] for s in signals]

    if direction == "bullish":
        # 1. Close above rising 50 MA (demand is present, not just a bounce)
        if df is not None and len(df) >= 55:
            ma50 = df["Close"].rolling(window=50).mean()
            if (not pd.isna(ma50.iloc[-1]) and not pd.isna(ma50.iloc[-5])
                    and df["Close"].iloc[-1] > ma50.iloc[-1]
                    and ma50.iloc[-1] > ma50.iloc[-5]):
                conditions_met += 1

        # 2. Higher low vs last swing (structure is bullish)
        if df is not None and len(df) >= 30:
            recent_low = df["Low"].iloc[-10:].min()
            prior_low = df["Low"].iloc[-30:-10].min()
            if recent_low > prior_low:
                conditions_met += 1

        # 3. Weekly trend bullish
        if htf_trend == "bullish":
            conditions_met += 1

        # 4. Has a structure/demand signal (not just oscillator bounce)
        demand_signals = {"Support/Resist", "EMA21 Pullback", "200 MA", "MA Cross", "VCP Breakout", "Accumulation"}
        if any(n in demand_signals for n in indicator_names):
            conditions_met += 1

        # 5. Confirmed reversal structure (2+ reversal signals = bottom is real)
        reversal_signals = {"Double Bottom", "Failed Breakdown", "RSI Divergence",
                            "Volume Climax", "Accumulation", "Mean Reversion"}
        if sum(1 for n in indicator_names if n in reversal_signals) >= 2:
            conditions_met += 1

        # 6. Volume surge — 2x+ avg volume = institutional commitment
        if vol_ratio >= 2.0:
            conditions_met += 1

        # 7. Market alignment — SPY/QQQ trending with your direction
        if market_agrees(market_trends, direction):
            conditions_met += 1

        # 8. RS outperformance — stock stronger than SPY despite downtrend
        if rs_raw is not None and rs_raw > 5:
            conditions_met += 1

    else:  # bearish
        # 1. Close below falling 50 MA
        if df is not None and len(df) >= 55:
            ma50 = df["Close"].rolling(window=50).mean()
            if (not pd.isna(ma50.iloc[-1]) and not pd.isna(ma50.iloc[-5])
                    and df["Close"].iloc[-1] < ma50.iloc[-1]
                    and ma50.iloc[-1] < ma50.iloc[-5]):
                conditions_met += 1

        # 2. Lower high vs last swing
        if df is not None and len(df) >= 30:
            recent_high = df["High"].iloc[-10:].max()
            prior_high = df["High"].iloc[-30:-10].max()
            if recent_high < prior_high:
                conditions_met += 1

        # 3. Weekly trend bearish
        if htf_trend == "bearish":
            conditions_met += 1

        # 4. Has a structure/supply signal
        supply_signals = {"Support/Resist", "EMA21 Pullback", "200 MA", "MA Cross", "VCP Breakout", "Accumulation"}
        if any(n in supply_signals for n in indicator_names):
            conditions_met += 1

        # 5. Confirmed reversal structure (2+ reversal signals = top is real)
        reversal_signals = {"Double Top", "Failed Breakout", "RSI Divergence",
                            "Volume Climax", "Accumulation", "Mean Reversion"}
        if sum(1 for n in indicator_names if n in reversal_signals) >= 2:
            conditions_met += 1

        # 6. Volume surge — 2x+ avg volume = institutional commitment
        if vol_ratio >= 2.0:
            conditions_met += 1

        # 7. Market alignment — SPY/QQQ trending with your direction
        if market_agrees(market_trends, direction):
            conditions_met += 1

        # 8. RS underperformance — stock weaker than SPY despite uptrend
        if rs_raw is not None and rs_raw < -5:
            conditions_met += 1

    return conditions_met


def _get_setup_context(signals: list) -> str:
    """Classify signal group as 'with_trend' or 'counter_trend'.
    Counts how many signals fall in each bucket; majority wins.
    Ties default to with_trend (benefit of the doubt)."""
    names = [s["indicator"] for s in signals]
    ct = sum(1 for n in names if n in COUNTER_TREND_SIGNALS)
    wt = sum(1 for n in names if n in WITH_TREND_SIGNALS)
    return "counter_trend" if ct > wt else "with_trend"


def calculate_score(signals: list, direction: str, trend_200: str,
                    htf_trend: str, market_trends: dict, vol_ratio: float,
                    df: pd.DataFrame = None, adaptive_weights: dict = None,
                    pillar_mults: dict = None,
                    rs_raw: float = None,
                    regime: str = "unknown") -> float:
    """Calculate a score out of 10 for a directional signal group."""

    # Adaptive pillar caps — learned from performance data
    pm = pillar_mults if pillar_mults else {k: 1.0 for k in PILLAR_DEFAULTS}
    ind_cap = MAX_INDICATOR_SCORE * pm.get("indicators", 1.0)
    trend_cap = MAX_TREND_SCORE * pm.get("trend", 1.0)
    vol_cap = MAX_VOLUME_SCORE * pm.get("volume", 1.0)
    conv_cap = MAX_CONVICTION_SCORE * pm.get("conviction", 1.0)

    # --- INDICATOR SCORE (max ind_cap) ---
    # Factor-based scoring: signals are grouped into factors (momentum, trend_ma,
    # structure, volume_flow, breakout, context, compound). Each factor's score
    # is its BEST signal plus a small confluence bonus for confirming signals in
    # the same factor. Then top 4 factors go through diminishing returns.
    _DECAY = [1.0, 0.8, 0.6, 0.45]
    _EXPECTED_PEAK = 0.80  # typical strong factor score with strength weighting
    _MAX_RAW = sum(d * _EXPECTED_PEAK for d in _DECAY)  # ~2.28
    _MAX_MULT = 1.15  # quality mult ceiling
    _NORM = ind_cap / (_MAX_RAW * _MAX_MULT)  # scale to ind_cap

    weights_source = adaptive_weights if adaptive_weights else SIGNAL_WEIGHTS
    # Collapse signals into factors — best signal + small confluence bonus
    factor_signals = {}  # factor_name -> list of effective weights
    for s in signals:
        name = s["indicator"]
        factor = SIGNAL_TO_FACTOR.get(name, "context")
        w = weights_source.get(name, 0.5)
        w *= s.get("strength", 1.0)
        # Apply adaptive factor weight if available
        if adaptive_weights and factor in adaptive_weights:
            fw = adaptive_weights[factor]
            w *= fw / FACTOR_WEIGHTS.get(factor, 1.0)  # scale relative to base
        factor_signals.setdefault(factor, []).append(w)
    # Within-factor confluence: best signal + 20% of up to 2 confirming signals
    factor_best = {}
    for factor, ws in factor_signals.items():
        ws_sorted = sorted(ws, reverse=True)
        best = ws_sorted[0]
        bonus = sum(w * 0.2 for w in ws_sorted[1:3])
        factor_best[factor] = min(1.0, best + bonus)
    factor_scores = sorted(factor_best.values(), reverse=True)
    top4 = factor_scores[:4]

    # Diminishing returns: first factor full value, each next one worth less
    raw_indicator = sum(w * d for w, d in zip(top4, _DECAY[:len(top4)]))

    # Quality multiplier: avg factor score → rewards quality over quantity
    # Calibrated for strength-weighted scores: avg 0.35 → 0.85, avg 0.55 → 1.0, avg 0.75+ → 1.15
    avg_q = sum(top4) / len(top4) if top4 else 0.5
    quality_mult = 0.85 + 0.30 * max(0.0, min(1.0, (avg_q - 0.35) / 0.40))

    indicator_score = min(raw_indicator * quality_mult * _NORM, ind_cap)

    indicator_names = [s["indicator"] for s in signals]

    # --- TREND ALIGNMENT (max trend_cap) ---
    trend_score = 0.0
    trend_agrees = (
        (direction == "bullish" and trend_200 == "bullish") or
        (direction == "bearish" and trend_200 == "bearish")
    )
    if trend_agrees:
        trend_score += 1.5

    htf_agrees = (htf_trend == direction)
    htf_neutral = (htf_trend == "neutral")
    if htf_agrees:
        trend_score += 1.0
    elif htf_neutral:
        trend_score += 0.25

    if market_agrees(market_trends, direction):
        trend_score += 0.5

    # Counter-trend discount: reversals get reduced trend credit
    # In chop, range plays aren't fighting a trend — softer discount
    setup_ctx = _get_setup_context(signals)
    if setup_ctx == "counter_trend":
        discount = CHOP_TREND_DISCOUNT if regime == "chop" else TREND_DISCOUNT_COUNTER
        trend_score *= discount

    trend_score = min(trend_score, trend_cap)

    # --- VOLUME (max vol_cap) ---
    volume_score = min(score_volume(vol_ratio), vol_cap)

    # --- CONVICTION BONUS (max conv_cap) ---
    # Requires structure signals — pure oscillator stacking doesn't count
    structure_set = {"Support/Resist", "EMA21 Pullback", "5 EMA Touch", "200 MA Touch", "200 MA", "MA Cross", "VCP Breakout", "Breakout Retest", "Channel Bounce", "Golden Cross", "Failed Breakdown", "Failed Breakout", "Double Bottom", "Double Top", "M on Uptrend", "W on Downtrend", "M on Downtrend", "M Breakout Up", "M Breakout Down", "Accumulation", "Range Compression", "Trendline Channel"}
    structure_count = sum(1 for n in indicator_names if n in structure_set)
    conviction = 0.0
    if structure_count >= 2 and len(signals) >= 4:
        conviction = 1.0
    elif structure_count >= 1 and len(signals) >= 4:
        conviction = 0.5
    elif len(signals) >= 5:
        conviction = 0.5  # lots of signals but no structure = half credit
    conviction = min(conviction, conv_cap)

    # --- ACCUMULATION / HIGHER LOWS VOLUME FLOOR ---
    # These signals inherently confirm directional volume over 20 days.
    # A quiet single day doesn't negate weeks of institutional buying/selling.
    _vol_confirmed = {"Accumulation", "Distribution", "Higher Lows", "Dist Breakdown"}
    if _vol_confirmed & set(indicator_names):
        volume_score = max(volume_score, 0.5)

    # --- CHANNEL QUALITY ADJUSTMENT ---
    # Trendline Channel is a self-contained trade thesis: the channel structure
    # defines entry, stop, and target. Adjustments are conservative and scale
    # with quality — only pristine channels get the full lift.
    chan_bonus = 0.0
    if "Trendline Channel" in indicator_names:
        tc_sig = next((s for s in signals if s["indicator"] == "Trendline Channel"), None)
        if tc_sig:
            meta = tc_sig.get("meta", {})
            min_touches = min(meta.get("n_peaks", 0), meta.get("n_valleys", 0))
            err_key = "avg_support_err" if direction == "bullish" else "avg_resist_err"
            avg_err = meta.get(err_key, 1.0)

            # Volume floor: low volume at channel boundary is not weakness
            # 0.5 = neutral (not penalized, not rewarded)
            volume_score = max(volume_score, 0.5)

            # Self-conviction: scales with touch count
            # 3 touches = partial, 4+ = full conviction
            if min_touches >= 4:
                conviction = max(conviction, conv_cap)
            else:
                conviction = max(conviction, conv_cap * 0.5)

            # Pattern quality bonus: only high-quality channels earn extra credit
            if min_touches >= 4:
                chan_bonus = 1.0  # 4+ touches per side = proven
                if avg_err < 0.5:
                    chan_bonus += 0.5  # excellent trendline fit
            else:
                chan_bonus = 0.5  # 3 touches = moderate base

    total = indicator_score + trend_score + volume_score + conviction + chan_bonus
    total = min(total, 10.0)

    # --- TREND STRENGTH GATE ---
    # Prevents falling knives / dead cat bounces from scoring high.
    # Softer in chop — range plays don't need strong trending conditions.
    # Channel plays exempt — the channel structure IS the trend confirmation
    # (proven support/resistance, not directional).
    trend_strength = check_trend_strength(
        signals, direction, trend_200, htf_trend, market_trends, df,
        vol_ratio=vol_ratio, rs_raw=rs_raw,
    )
    gate_min = CHOP_TREND_GATE_MIN if regime == "chop" else TREND_GATE_MIN
    is_channel_play = "Trendline Channel" in indicator_names
    if trend_strength < gate_min and not is_channel_play:
        total = min(total, TREND_GATE_CAP)

    # Build effective weight per signal (what the scoring actually used)
    effective_weights = {}
    for s in signals:
        name = s["indicator"]
        factor = SIGNAL_TO_FACTOR.get(name, "context")
        w = weights_source.get(name, 0.5)
        w *= s.get("strength", 1.0)
        if adaptive_weights and factor in adaptive_weights:
            fw = adaptive_weights[factor]
            w *= fw / FACTOR_WEIGHTS.get(factor, 1.0)
        effective_weights[name] = round(w, 4)

    breakdown = {
        "indicators": round(indicator_score, 1),
        "trend": round(trend_score, 1),
        "volume": round(volume_score, 1),
        "conviction": round(conviction, 1),
        "effective_weights": effective_weights,
    }

    return round(total, 1), breakdown


def estimate_win_rate(score: float, vol_ratio: float = 1.0,
                      trend_agrees: bool = True, mkt_aligned: bool = True,
                      htf_agrees: bool = True, rs_raw: float = None,
                      sector_status: str = None,
                      adaptive_wr: dict = None,
                      regime: str = None,
                      setup_context: str = None,
                      preference_boost: float = 0.0,
                      direction: str = "bullish",
                      has_opposing_signals: bool = False,
                      near_52w_high_overbought: bool = False,
                      divergence_override: bool = False,
                      intel: dict = None,
                      price: float = 0) -> float:
    """Estimate win probability — blends sigmoid prior with empirical data.
    When adaptive_wr is provided, uses learned win rates by score bucket.
    Sigmoid is the cold-start prior; real data takes over as history grows."""
    # Base rate: adaptive curve if available, calibrated sigmoid fallback
    sig_p = get_sigmoid_params()
    if adaptive_wr:
        bucket = round(score * 2) / 2
        bucket = max(4.0, min(10.0, bucket))
        if bucket in adaptive_wr:
            rate = adaptive_wr[bucket]["blended"]
        else:
            sigmoid = 1.0 / (1.0 + math.exp(-sig_p["slope"] * (score - sig_p["midpoint"])))
            rate = sig_p["floor"] + sigmoid * (sig_p["ceiling"] - sig_p["floor"])
    else:
        sigmoid = 1.0 / (1.0 + math.exp(-sig_p["slope"] * (score - sig_p["midpoint"])))
        rate = sig_p["floor"] + sigmoid * (sig_p["ceiling"] - sig_p["floor"])

    # Contextual adjustments (these layer on top of the base)
    if vol_ratio < 0.8:
        rate -= 0.07
    elif vol_ratio < 1.0:
        rate -= 0.03
    elif vol_ratio >= 2.0:
        rate += 0.05

    if not trend_agrees:
        rate -= 0.10

    if not mkt_aligned:
        rate -= DIVERGENCE_MKT_WR_PENALTY if divergence_override else 0.15

    if not htf_agrees:
        rate -= 0.04

    # RS adjustments — will be overridden by adaptive version if data exists (Task 4)
    if rs_raw is not None:
        rs_adj = _get_adaptive_rs_adjustment(rs_raw)
        rate += rs_adj

    # Sector rotation — play-type aware: flipped for mean-reversion setups
    sector_adj = _get_adaptive_sector_adjustment(sector_status, setup_context)
    rate += sector_adj

    # 52-week high + overbought caution — chasing into exhaustion
    if near_52w_high_overbought:
        rate -= HIGH_52W_OVERBOUGHT_WR_PENALTY

    # Adverse condition stacking — multiple headwinds compound the problem
    # Each individual penalty above handles its own condition, but 3 headwinds at once
    # should hurt more than the sum of parts
    _wrong_sector = (
        (sector_status == "lagging" and direction == "bullish")
        or (sector_status == "leading" and direction == "bearish")
    ) if sector_status else False
    adverse_count = sum([
        not trend_agrees,
        not mkt_aligned,
        not htf_agrees,
        _wrong_sector,
        bool(vol_ratio and vol_ratio < 1.0),
        has_opposing_signals,
    ])
    if adverse_count >= 2:
        rate -= ADVERSE_STACK_PENALTY * (adverse_count - 1)

    # User preference boost (learned from manual favorite selections)
    rate += preference_boost

    # Market intelligence adjustment
    if intel:
        rate += compute_intel_wr_adjustment(intel, direction, price)

    return max(0.15, min(0.75, rate))


def compute_intel_adjustment(intel: dict, direction: str, price: float = 0) -> dict:
    """Compute score adjustment from market intelligence factors.
    Returns dict with per-factor adjustments and total.
    Total clamped to [-0.3, +0.3] — these are context, not primary signals."""
    if not intel:
        return {"total": 0.0}
    adj = {}
    is_bull = direction == "bullish"

    # 1. SHORT INTEREST
    si = intel.get("short_pct_float")
    if si is not None:
        if is_bull:
            if si > 0.20:
                adj["short_interest"] = 0.10   # squeeze candidate
            elif si > 0.10:
                adj["short_interest"] = 0.05
            else:
                adj["short_interest"] = 0.0
        else:
            if si > 0.20:
                adj["short_interest"] = -0.05  # crowded short, squeeze risk
            elif si > 0.10:
                adj["short_interest"] = 0.05   # shorts confirm thesis
            else:
                adj["short_interest"] = 0.0

    # 2. FLOAT SIZE
    flt = intel.get("float_shares")
    if flt is not None:
        if flt < 20_000_000:
            adj["float"] = 0.05       # micro float amplifies moves
        elif flt < 50_000_000:
            adj["float"] = 0.03       # small float
        elif flt > 2_000_000_000:
            adj["float"] = -0.03      # mega float dampens
        else:
            adj["float"] = 0.0

    # 3. ANALYST TARGETS
    target = intel.get("target_mean_price")
    n_analysts = intel.get("analyst_count")
    if target and n_analysts and n_analysts >= 3 and price > 0:
        upside = (target - price) / price
        if is_bull:
            if upside > 0.25:
                adj["analyst_target"] = 0.10
            elif upside > 0.10:
                adj["analyst_target"] = 0.05
            elif upside < -0.10:
                adj["analyst_target"] = -0.05
            else:
                adj["analyst_target"] = 0.0
        else:
            if upside < -0.10:
                adj["analyst_target"] = 0.05
            elif upside > 0.25:
                adj["analyst_target"] = -0.10  # analysts very bullish = risky short
            elif upside > 0.10:
                adj["analyst_target"] = -0.05
            else:
                adj["analyst_target"] = 0.0

    # 4. INSTITUTIONAL OWNERSHIP
    inst = intel.get("inst_pct")
    if inst is not None:
        if is_bull:
            if inst > 0.80:
                adj["inst_own"] = 0.05    # institutional stability
            elif inst > 0.50:
                adj["inst_own"] = 0.03
            elif inst < 0.15:
                adj["inst_own"] = -0.03   # speculative
            else:
                adj["inst_own"] = 0.0
        else:
            if inst > 0.80:
                adj["inst_own"] = -0.05   # institutional floor under price
            elif inst < 0.15:
                adj["inst_own"] = 0.03    # vulnerable to downside
            else:
                adj["inst_own"] = 0.0

    # 5. BETA
    beta = intel.get("beta")
    if beta is not None:
        if beta > 1.5:
            adj["beta"] = 0.05       # high beta amplifies setups
        elif beta > 1.2:
            adj["beta"] = 0.03
        elif beta < 0.5:
            adj["beta"] = -0.03      # sluggish response
        else:
            adj["beta"] = 0.0

    total = sum(adj.values())
    total = max(-0.30, min(0.30, total))
    adj["total"] = round(total, 2)
    return adj


def compute_intel_wr_adjustment(intel: dict, direction: str, price: float = 0) -> float:
    """Win rate adjustment from market intelligence. Returns float in [-0.03, +0.03]."""
    score_adj = compute_intel_adjustment(intel, direction, price)
    wr_adj = score_adj.get("total", 0) / 10.0
    return max(-0.03, min(0.03, wr_adj))


def calculate_ev(score: float, risk: float, tp1: float, tp2: float,
                 entry: float, direction: str, vol_ratio: float = 1.0,
                 trend_agrees: bool = True, mkt_aligned: bool = True,
                 htf_agrees: bool = True, rs_raw: float = None,
                 sector_status: str = None, adaptive_wr: dict = None,
                 regime: str = None, setup_context: str = None,
                 preference_boost: float = 0.0,
                 has_opposing_signals: bool = False,
                 near_52w_high_overbought: bool = False,
                 divergence_override: bool = False,
                 intel: dict = None) -> dict:
    """Calculate expected value per trade.
    Uses blended reward: 70% exit at TP1 (1R), 30% hold to TP2 (2R).
    This models realistic partial profit-taking."""
    win_rate = estimate_win_rate(score, vol_ratio, trend_agrees, mkt_aligned, htf_agrees,
                                 rs_raw, sector_status, adaptive_wr=adaptive_wr,
                                 regime=regime, setup_context=setup_context,
                                 preference_boost=preference_boost,
                                 direction=direction,
                                 has_opposing_signals=has_opposing_signals,
                                 near_52w_high_overbought=near_52w_high_overbought,
                                 divergence_override=divergence_override,
                                 intel=intel, price=entry)
    loss_rate = 1.0 - win_rate

    if direction == "bullish":
        r1 = tp1 - entry
        r2 = tp2 - entry
    else:
        r1 = entry - tp1
        r2 = entry - tp2
    r1 = max(r1, 0.01)
    r2 = max(r2, 0.01)

    # Blended avg win: 70% take profit at 1R, 30% hold to 2R
    avg_win = (r1 * 0.70) + (r2 * 0.30)
    avg_loss = risk

    ev_per_share = (win_rate * avg_win) - (loss_rate * avg_loss)
    ev_ratio = ev_per_share / avg_loss if avg_loss > 0 else 0
    return {
        "win_rate": round(win_rate * 100, 0),
        "ev_per_share": round(ev_per_share, 2),
        "ev_ratio": round(ev_ratio, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
    }


def get_tier(score: float) -> str:
    if score >= TIER_A_MIN:
        return "A"
    elif score >= TIER_B_MIN:
        return "B"
    elif score >= TIER_C_MIN:
        return "C"
    return ""


def get_tier_label(tier: str) -> str:
    return {"A": "A-TIER  [TOP SETUP]", "B": "B-TIER  [SOLID]", "C": "C-TIER  [WATCHLIST]"}.get(tier, "")


def kelly_fraction(win_rate_pct: float, avg_win: float, avg_loss: float,
                   scale: float = 0.25) -> float:
    """Quarter-Kelly position sizing. Returns fraction of account to risk.
    win_rate_pct: win rate as percentage (0-100).
    avg_win/avg_loss: dollar amounts (avg_loss > 0).
    scale: Kelly fraction (0.25 = quarter-Kelly for safety).
    Returns 0.0 to 0.02 (max 2% of account)."""
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    wr = win_rate_pct / 100.0
    b = avg_win / avg_loss
    kelly = (wr * b - (1 - wr)) / b
    return min(0.02, max(0.0, kelly * scale))


def get_risk_tier(score: float, trade_type: str,
                  ev_ratio: float = 0.0, win_rate: float = 0.0,
                  avg_win: float = 0.0, avg_loss: float = 0.0) -> dict:
    """Suggest position size tier based on score, trade type, and EV.
    Also computes Kelly fraction as additive sizing info."""
    if score >= TIER_A_MIN:
        size = "FULL 1%"
        desc = "High conviction — full risk"
    elif score >= 7.0:
        size = "FULL 1%"
        desc = "Strong setup — full risk"
    elif score >= TIER_B_MIN:
        size = "HALF 0.5%"
        desc = "Solid but not perfect — half size"
    elif score >= 5.0:
        size = "QUARTER 0.25%"
        desc = "Developing — starter position"
    else:
        size = "QUARTER 0.25%"
        desc = "Watchlist level — test the bounce"

    # Downgrade counter-trend trades
    if "counter-trend" in trade_type or "against 200 MA" in trade_type:
        if "FULL" in size:
            size = "HALF 0.5%"
            desc += " (downgraded: counter-trend)"
        elif "HALF" in size:
            size = "QUARTER 0.25%"
            desc += " (downgraded: counter-trend)"

    # EV gate: don't size above QUARTER unless edge is real
    if size != "QUARTER 0.25%" and ev_ratio < EV_SIZE_MIN and win_rate < EV_SIZE_WIN_MIN:
        size = "QUARTER 0.25%"
        desc += " (capped: EV/win% below threshold)"

    # Kelly criterion — additive info alongside score-based tiers
    kf = kelly_fraction(win_rate, avg_win, avg_loss)
    if kf >= 0.015:
        kelly_tier = "FULL"
    elif kf >= 0.008:
        kelly_tier = "HALF"
    elif kf >= 0.003:
        kelly_tier = "QUARTER"
    else:
        kelly_tier = "SKIP"

    return {"size": size, "desc": desc,
            "kelly_pct": round(kf * 100, 2), "kelly_tier": kelly_tier}
