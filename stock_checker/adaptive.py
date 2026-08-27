import json
import math
from datetime import date

from datetime import datetime as _datetime

from .constants import (
    SIGNAL_WEIGHTS, PILLAR_DEFAULTS, PICKS_HISTORY_FILE,
    ADAPTIVE_STATE_FILE, ADAPTIVE_ALPHA, REPORTS_DIR,
    TIER_ADAPTIVE_WEIGHTS, WINNERS_JSON, LOSERS_JSON,
    PLAY_TYPES, SIGNAL_DAMPENING,
    SIGNAL_TO_FACTOR, FACTOR_WEIGHTS,
    DEFAULT_SIGMOID_PARAMS,
)


# ---------------------------------------------------------------------------
# Adaptive learning — incremental running stats
# ---------------------------------------------------------------------------
# Instead of re-reading all raw picks every run, we store running statistics
# (weighted win counts per dimension) in .adaptive_state.json.  When a pick
# is scored, consume_scored_picks() folds its outcome into the stats and
# removes it from .picks_history.json.  Adaptive functions read from the
# pre-aggregated stats.  Bulk exponential decay (60d half-life) replaces
# per-pick time decay.
# ---------------------------------------------------------------------------


def _time_decay_weight(scan_date_str: str, half_life_days: int = 60) -> float:
    """Exponential decay: recent results weighted higher than old ones.
    half_life_days=60 means a pick from 60 days ago counts half as much as today's.
    Used only during bootstrap migration."""
    try:
        scan_d = date.fromisoformat(scan_date_str)
        days_ago = (date.today() - scan_d).days
        return math.exp(-0.693 * days_ago / half_life_days)  # ln(2) ≈ 0.693
    except Exception:
        return 0.5  # fallback: medium weight


def _load_adaptive_state() -> dict:
    """Load previous adaptive output for EMA damping between runs."""
    if not ADAPTIVE_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ADAPTIVE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_adaptive_state(state: dict):
    """Persist adaptive output so next run can dampen toward it."""
    state["last_updated"] = date.today().isoformat()
    ADAPTIVE_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_cached_adaptive_values(regime: str) -> dict:
    """Return last-saved adaptive values without recomputing or dampening.
    Used when no new picks were consumed — prevents phantom drift."""
    state = _load_adaptive_state()
    # Signal weights
    key = f"regime_weights_{regime}"
    wts = state.get(key, state.get("regime_weights_unknown", {}))
    if not wts:
        wts = dict(SIGNAL_WEIGHTS)
    # Pillar mults
    pillars = state.get("pillar_mults", {k: 1.0 for k in PILLAR_DEFAULTS})
    # Win rate curve
    wr_curve = state.get("win_rate_curve", {})
    wr = {}
    for k, v in wr_curve.items():
        wr[float(k)] = {"blended": v, "count": 0, "empirical": v}
    # Stop multiplier
    stop = state.get("stop_multiplier", 1.5)
    # Distrib threshold
    distrib = state.get("distrib_threshold", 0.77)
    return {
        "weights": wts,
        "pillars": pillars,
        "win_rates": wr,
        "stop_mult": stop,
        "distrib_thresh": distrib,
    }


MIN_PLAY_SAMPLES = 5  # minimum picks before trusting play type win rate


def get_play_type_win_rates() -> dict:
    """Return {play_type: win_rate} for play types with enough data.
    Win rate is weighted (w_wins / w_total). Only includes play types
    with >= MIN_PLAY_SAMPLES picks so we don't chase noise."""
    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs:
        return {}
    pt_stats = rs.get("play_types", {})
    result = {}
    for pt, data in pt_stats.items():
        if data.get("count", 0) >= MIN_PLAY_SAMPLES and data.get("w_total", 0) > 0:
            result[pt] = data["w_wins"] / data["w_total"]
    return result


def _damp_value(new_val: float, prev_val: float, alpha: float = None) -> float:
    """EMA blend: move alpha fraction from prev toward new each run."""
    if alpha is None:
        alpha = ADAPTIVE_ALPHA
    return prev_val + alpha * (new_val - prev_val)


def _damp_dict(new_dict: dict, prev_dict: dict, alpha: float = None) -> dict:
    """EMA blend each numeric value in a dict against its previous value."""
    if alpha is None:
        alpha = ADAPTIVE_ALPHA
    result = dict(new_dict)
    for k in result:
        if k in prev_dict and isinstance(result[k], (int, float)) and isinstance(prev_dict[k], (int, float)):
            result[k] = round(_damp_value(result[k], prev_dict[k], alpha), 4)
    return result


def _beta_confidence(w_wins, w_total, prior_alpha=2.0, prior_beta=2.0):
    """Beta-Binomial posterior: principled confidence from sample size.
    Returns (posterior_mean, confidence) where confidence in [0, 1].
    posterior_mean blends empirical rate with prior as data grows.
    confidence = w_total / (w_total + prior_strength) — natural ramp
    that replaces all ad-hoc linear confidence ramps."""
    prior_strength = prior_alpha + prior_beta
    if w_total <= 0:
        return prior_alpha / prior_strength, 0.0
    post_alpha = prior_alpha + w_wins
    post_beta = prior_beta + (w_total - w_wins)
    posterior_mean = post_alpha / (post_alpha + post_beta)
    confidence = w_total / (w_total + prior_strength)
    return posterior_mean, confidence


# Module-level cache (populated once per scan, avoids repeated file reads)
_module_cache = {}


def _fit_sigmoid_params(buckets: dict) -> dict:
    """Fit sigmoid parameters (midpoint, slope, floor, ceiling) from
    blended score-bucket win rates. Returns clamped params."""
    # Collect buckets with enough data for fitting
    data = [(score, info["blended"], info["count"])
            for score, info in sorted(buckets.items())
            if isinstance(info, dict) and info.get("count", 0) >= 3]

    if len(data) < 4:
        return dict(DEFAULT_SIGMOID_PARAMS)

    # Floor: weighted avg of bottom 2 buckets with data
    bottom = data[:2]
    bw = sum(d[2] for d in bottom)
    floor_val = sum(d[1] * d[2] for d in bottom) / bw if bw > 0 else 0.20
    floor_val = max(0.15, min(0.35, floor_val))

    # Ceiling: weighted avg of top 2 buckets with data
    top = data[-2:]
    tw = sum(d[2] for d in top)
    ceil_val = sum(d[1] * d[2] for d in top) / tw if tw > 0 else 0.75
    ceil_val = max(0.55, min(0.80, ceil_val))

    # Midpoint: score where rate crosses halfway between floor and ceiling
    halfway = (floor_val + ceil_val) / 2
    midpoint = 6.0
    for i in range(len(data) - 1):
        s1, r1, _ = data[i]
        s2, r2, _ = data[i + 1]
        if r1 <= halfway <= r2 and r2 > r1:
            midpoint = s1 + (halfway - r1) / (r2 - r1) * (s2 - s1)
            break
    midpoint = max(5.0, min(7.5, midpoint))

    # Slope: rate change per score point around midpoint
    near_mid = sorted(data, key=lambda d: abs(d[0] - midpoint))
    slope = 0.9
    if len(near_mid) >= 2:
        s1, r1, _ = near_mid[0]
        s2, r2, _ = near_mid[1]
        if abs(s2 - s1) > 0:
            raw_slope = abs(r2 - r1) / abs(s2 - s1)
            sig_range = ceil_val - floor_val
            if sig_range > 0:
                slope = raw_slope * 4 / sig_range
    slope = max(0.5, min(1.5, slope))

    return {
        "midpoint": round(midpoint, 2),
        "slope": round(slope, 2),
        "floor": round(floor_val, 3),
        "ceiling": round(ceil_val, 3),
    }


def get_sigmoid_params() -> dict:
    """Return calibrated sigmoid params (cached after first read per scan)."""
    if "sigmoid" in _module_cache:
        return _module_cache["sigmoid"]
    state = _load_adaptive_state()
    params = state.get("sigmoid_params", dict(DEFAULT_SIGMOID_PARAMS))
    _module_cache["sigmoid"] = params
    return params


# ---------------------------------------------------------------------------
# Running stats helpers
# ---------------------------------------------------------------------------

def _init_running_stats() -> dict:
    """Return an empty running stats structure."""
    return {
        "total": {"w_wins": 0.0, "w_total": 0.0, "count": 0},
        "signals": {},
        "play_types": {},
        "pillars_win": {},
        "pillars_loss": {},
        "win_count": 0,
        "loss_count": 0,
        "score_buckets": {},
        "rs_buckets": {},
        "rs_total_count": 0,
        "sector": {},
        "sector_total_count": 0,
        "regime_total": {},
        "regime_signals": {},
        "stop": {"w_wins": 0.0, "w_losses": 0.0, "count": 0},
        "adx_buckets": {},
        "adx_total_count": 0,
        "tiers": {},
        "directions": {},
        "trade_types": {},
        "tier_direction": {},
        "tier_trade_type": {},
        "c_tier_count": 0,
        "c_tier_near_b_count": 0,
        "last_consumed": None,
    }


def _decay_running_stats(rs: dict, decay: float):
    """Apply exponential decay to all w_* fields including w_count.
    Raw `count` fields are NOT decayed — kept for audit/display only."""
    _decay_keys = ("w_wins", "w_total", "w_count")

    for key in _decay_keys:
        if key in rs["total"]:
            rs["total"][key] *= decay

    for sig_data in rs.get("signals", {}).values():
        for key in _decay_keys:
            if key in sig_data:
                sig_data[key] *= decay

    for pt_data in rs.get("play_types", {}).values():
        for key in _decay_keys:
            if key in pt_data:
                pt_data[key] *= decay

    for target in ("pillars_win", "pillars_loss"):
        for pillar_data in rs.get(target, {}).values():
            for key in ("w_sum", "w_weight"):
                pillar_data[key] *= decay

    for bucket_data in rs.get("score_buckets", {}).values():
        for key in _decay_keys:
            if key in bucket_data:
                bucket_data[key] *= decay

    for bucket_data in rs.get("rs_buckets", {}).values():
        for key in _decay_keys:
            if key in bucket_data:
                bucket_data[key] *= decay

    for bucket_data in rs.get("sector", {}).values():
        for key in _decay_keys:
            if key in bucket_data:
                bucket_data[key] *= decay

    for bucket_data in rs.get("regime_total", {}).values():
        for key in _decay_keys:
            if key in bucket_data:
                bucket_data[key] *= decay

    for regime_data in rs.get("regime_signals", {}).values():
        for sig_data in regime_data.values():
            for key in _decay_keys:
                if key in sig_data:
                    sig_data[key] *= decay

    for key in ("w_wins", "w_losses", "w_count"):
        if key in rs["stop"]:
            rs["stop"][key] *= decay

    # Per-regime stop buckets
    for regime_stop_data in rs.get("regime_stop", {}).values():
        for key in ("w_wins", "w_losses", "w_count"):
            if key in regime_stop_data:
                regime_stop_data[key] *= decay

    for bucket_data in rs.get("adx_buckets", {}).values():
        for key in _decay_keys:
            if key in bucket_data:
                bucket_data[key] *= decay

    # Market intelligence buckets
    for bucket_name in ("si_buckets", "float_buckets", "analyst_buckets",
                        "inst_buckets", "beta_buckets"):
        for bucket_data in rs.get(bucket_name, {}).values():
            for key in _decay_keys:
                if key in bucket_data:
                    bucket_data[key] *= decay

    # Factor buckets (Phase 2)
    for factor_data in rs.get("factors", {}).values():
        for key in _decay_keys:
            if key in factor_data:
                factor_data[key] *= decay


def _classify_rs_bucket(rs_raw: float) -> str:
    """Classify RS value into a bucket name."""
    if rs_raw > 10:
        return "strong_leader"
    if rs_raw > 5:
        return "leader"
    if rs_raw < -10:
        return "strong_laggard"
    if rs_raw < -5:
        return "laggard"
    return "neutral"


def _ingest_pick(rs: dict, pick: dict, weight: float):
    """Add one scored pick's outcome to all running stat buckets.
    `weight` is typically tier_weight (during normal consumption)
    or tier_weight * time_decay (during bootstrap).
    Excluded picks get 0x weight — user flagged as non-signal event.
    (Favorites no longer amplify learning — preference system handles user taste.)"""
    if pick.get("excluded"):
        return  # excluded picks don't affect adaptive learning
    if pick.get("near_miss"):
        weight *= 0.3  # signal was right about direction, wrong about holding period

    is_day = pick.get("trade_type", "").startswith("DAY TRADE")
    result_key = "result_1d" if is_day else "result_10d"
    result = pick.get(result_key)
    is_win = result == "win"

    # --- total ---
    rs["total"]["w_total"] += weight
    rs["total"]["w_count"] = rs["total"].get("w_count", 0) + weight
    rs["total"]["count"] += 1
    if is_win:
        rs["total"]["w_wins"] += weight

    # --- signals ---
    tier = pick.get("tier", "C")
    for sig in pick.get("signal_indicators", []):
        bucket = rs["signals"].setdefault(sig, {"w_wins": 0.0, "w_total": 0.0, "count": 0, "wins": 0})
        bucket["w_total"] += weight
        bucket["w_count"] = bucket.get("w_count", 0) + weight
        bucket["count"] += 1
        if is_win:
            bucket["w_wins"] += weight
            bucket["wins"] = bucket.get("wins", 0) + 1
        # per-tier signal tracking
        tier_sigs = rs.setdefault("tier_signals", {}).setdefault(tier, {})
        ts = tier_sigs.setdefault(sig, {"wins": 0, "losses": 0, "count": 0})
        ts["count"] += 1
        if is_win:
            ts["wins"] += 1
        else:
            ts["losses"] += 1

    # --- factors (deduplicated — each factor counted once per pick) ---
    _seen_factors = set()
    for sig in pick.get("signal_indicators", []):
        factor = SIGNAL_TO_FACTOR.get(sig, "context")
        if factor in _seen_factors:
            continue
        _seen_factors.add(factor)
        fb = rs.setdefault("factors", {}).setdefault(
            factor, {"w_wins": 0.0, "w_total": 0.0, "count": 0, "wins": 0, "w_count": 0})
        fb["w_total"] += weight
        fb["w_count"] = fb.get("w_count", 0) + weight
        fb["count"] += 1
        if is_win:
            fb["w_wins"] += weight
            fb["wins"] = fb.get("wins", 0) + 1

    # --- play types (all tags, not just primary) ---
    for pt in pick.get("play_tags", [pick.get("play_type", "multi_signal")]):
        pb = rs.setdefault("play_types", {}).setdefault(pt, {"w_wins": 0.0, "w_total": 0.0, "count": 0, "wins": 0})
        pb["w_total"] += weight
        pb["w_count"] = pb.get("w_count", 0) + weight
        pb["count"] += 1
        if is_win:
            pb["w_wins"] += weight
            pb["wins"] = pb.get("wins", 0) + 1
        # per-tier play tracking
        tier_plays = rs.setdefault("tier_plays", {}).setdefault(tier, {})
        tp = tier_plays.setdefault(pt, {"wins": 0, "losses": 0, "count": 0})
        tp["count"] += 1
        if is_win:
            tp["wins"] += 1
        else:
            tp["losses"] += 1

    # --- pillars (breakdown values) ---
    breakdown = pick.get("breakdown")
    if breakdown:
        target = rs["pillars_win"] if is_win else rs["pillars_loss"]
        for pillar, value in breakdown.items():
            if not isinstance(value, (int, float)):
                continue
            pb = target.setdefault(pillar, {"w_sum": 0.0, "w_weight": 0.0})
            pb["w_sum"] += value * weight
            pb["w_weight"] += weight
        if is_win:
            rs["win_count"] += 1
        else:
            rs["loss_count"] += 1

    # --- score buckets ---
    score = pick.get("score", 0)
    sb_val = max(4.0, min(10.0, round(score * 2) / 2))
    sb_key = str(sb_val)
    bucket = rs["score_buckets"].setdefault(sb_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
    bucket["w_total"] += weight
    bucket["w_count"] = bucket.get("w_count", 0) + weight
    bucket["count"] += 1
    if is_win:
        bucket["w_wins"] += weight

    # --- RS buckets ---
    rs_raw = pick.get("rs_raw")
    if rs_raw is not None:
        rb_name = _classify_rs_bucket(rs_raw)
        rb = rs["rs_buckets"].setdefault(rb_name, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        rb["w_total"] += weight
        rb["w_count"] = rb.get("w_count", 0) + weight
        rb["count"] += 1
        if is_win:
            rb["w_wins"] += weight
        rs["rs_total_count"] += 1

    # --- sector ---
    sector_status = pick.get("sector_status")
    if sector_status:
        sg = sector_status if sector_status in ("leading", "neutral", "lagging") else "neutral"
        sb = rs["sector"].setdefault(sg, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        sb["w_total"] += weight
        sb["w_count"] = sb.get("w_count", 0) + weight
        sb["count"] += 1
        if is_win:
            sb["w_wins"] += weight
        rs["sector_total_count"] += 1

    # --- regime total + regime signals ---
    regime = pick.get("regime")
    if regime:
        rt = rs["regime_total"].setdefault(regime, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        rt["w_total"] += weight
        rt["w_count"] = rt.get("w_count", 0) + weight
        rt["count"] += 1
        if is_win:
            rt["w_wins"] += weight
        regime_sigs = rs["regime_signals"].setdefault(regime, {})
        for sig in pick.get("signal_indicators", []):
            rsb = regime_sigs.setdefault(sig, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
            rsb["w_total"] += weight
            rsb["w_count"] = rsb.get("w_count", 0) + weight
            rsb["count"] += 1
            if is_win:
                rsb["w_wins"] += weight

    # --- stop (global + per-regime) ---
    rs["stop"]["w_count"] = rs["stop"].get("w_count", 0) + weight
    rs["stop"]["count"] += 1
    if is_win:
        rs["stop"]["w_wins"] += weight
    else:
        rs["stop"]["w_losses"] += weight
    # Per-regime stop tracking
    regime = pick.get("regime")
    if regime:
        regime_stops = rs.setdefault("regime_stop", {})
        rsb = regime_stops.setdefault(regime, {"w_wins": 0.0, "w_losses": 0.0, "count": 0, "w_count": 0})
        rsb["w_count"] = rsb.get("w_count", 0) + weight
        rsb["count"] += 1
        if is_win:
            rsb["w_wins"] += weight
        else:
            rsb["w_losses"] += weight

    # --- ADX buckets ---
    adx = pick.get("regime_adx")
    if adx and adx > 0:
        ab_key = str(int(adx // 3) * 3)
        ab = rs["adx_buckets"].setdefault(ab_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        ab["w_total"] += weight
        ab["w_count"] = ab.get("w_count", 0) + weight
        ab["count"] += 1
        if is_win:
            ab["w_wins"] += weight
        rs["adx_total_count"] += 1

    # --- distrib vol ratio buckets (for adaptive threshold) ---
    dvr = pick.get("distrib_vol_ratio")
    if dvr is not None:
        dvr_key = str(round(dvr * 20) / 20)  # bucket to nearest 0.05
        dvb = rs.setdefault("distrib_vol_buckets", {}).setdefault(dvr_key, {"wins": 0, "losses": 0, "count": 0})
        dvb["count"] += 1
        if is_win:
            dvb["wins"] += 1
        else:
            dvb["losses"] += 1

    # --- Market intelligence buckets ---
    si = pick.get("short_pct_float")
    if si is not None:
        si_key = str(round(si * 50) / 50)  # bucket to nearest 0.02
        sb = rs.setdefault("si_buckets", {}).setdefault(si_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        sb["w_total"] += weight
        sb["w_count"] = sb.get("w_count", 0) + weight
        sb["count"] += 1
        if is_win:
            sb["w_wins"] += weight

    flt = pick.get("float_shares")
    if flt is not None and flt > 0:
        flt_m = flt / 1_000_000
        if flt_m < 20:
            flt_key = "micro_<20M"
        elif flt_m < 50:
            flt_key = "small_20-50M"
        elif flt_m < 200:
            flt_key = "mid_50-200M"
        elif flt_m < 1000:
            flt_key = "large_200M-1B"
        else:
            flt_key = "mega_>1B"
        fb = rs.setdefault("float_buckets", {}).setdefault(flt_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        fb["w_total"] += weight
        fb["w_count"] = fb.get("w_count", 0) + weight
        fb["count"] += 1
        if is_win:
            fb["w_wins"] += weight

    au = pick.get("analyst_upside_pct")
    if au is not None:
        au_key = str(round(au * 20) / 20)  # bucket to nearest 0.05
        ab = rs.setdefault("analyst_buckets", {}).setdefault(au_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        ab["w_total"] += weight
        ab["w_count"] = ab.get("w_count", 0) + weight
        ab["count"] += 1
        if is_win:
            ab["w_wins"] += weight

    inst = pick.get("inst_pct")
    if inst is not None:
        inst_key = str(round(inst * 10) / 10)  # bucket to nearest 0.10
        ib = rs.setdefault("inst_buckets", {}).setdefault(inst_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        ib["w_total"] += weight
        ib["w_count"] = ib.get("w_count", 0) + weight
        ib["count"] += 1
        if is_win:
            ib["w_wins"] += weight

    beta = pick.get("beta")
    if beta is not None:
        beta_key = str(round(beta * 5) / 5)  # bucket to nearest 0.2
        bb = rs.setdefault("beta_buckets", {}).setdefault(beta_key, {"w_wins": 0.0, "w_total": 0.0, "count": 0})
        bb["w_total"] += weight
        bb["w_count"] = bb.get("w_count", 0) + weight
        bb["count"] += 1
        if is_win:
            bb["w_wins"] += weight

    # --- tier / direction / trade_type (raw counts for metrics report) ---
    direction = pick.get("direction", "bullish")
    tt = "day" if is_day else "swing"

    for bucket_name, key in [("tiers", tier), ("directions", direction),
                              ("trade_types", tt),
                              ("tier_direction", f"{tier}_{direction}"),
                              ("tier_trade_type", f"{tier}_{tt}")]:
        b = rs.setdefault(bucket_name, {}).setdefault(key, {"wins": 0, "losses": 0})
        if is_win:
            b["wins"] += 1
        else:
            b["losses"] += 1

    # --- near-miss counter ---
    if pick.get("near_miss"):
        rs["near_miss_count"] = rs.get("near_miss_count", 0) + 1


def _dump_to_audit(picks: list):
    """Append consumed picks to winners.json / losers.json (write-only audit trail).
    These files are never read by the system — purely for human review."""
    wins = []
    losses = []
    for p in picks:
        is_day = p.get("trade_type", "").startswith("DAY TRADE")
        result_key = "result_1d" if is_day else "result_10d"
        result = p.get(result_key)
        if result == "win":
            wins.append(p)
        elif result == "loss":
            losses.append(p)
        # draws: not dumped — nothing happened

    for path, entries in [(WINNERS_JSON, wins), (LOSERS_JSON, losses)]:
        if not entries:
            continue
        existing = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.extend(entries)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Bootstrap + consumption
# ---------------------------------------------------------------------------

def _scan_for_earnings_gaps(picks: list) -> int:
    """Retroactively flag picks that were hit by earnings-like gaps.
    Groups by ticker to minimize API calls. Returns count of flagged picks."""
    import pandas as pd
    from .data import fetch_stock_data

    by_ticker = {}
    for p in picks:
        if p.get("earnings_gap") is not None:
            continue  # already checked
        t = p.get("ticker")
        if t:
            by_ticker.setdefault(t, []).append(p)

    flagged = 0
    for ticker, ticker_picks in by_ticker.items():
        try:
            df = fetch_stock_data(ticker, period="3mo")
            if df.empty:
                continue
        except Exception:
            continue

        for p in ticker_picks:
            scan_dt = pd.Timestamp(p["scan_date"])
            if df.index.tz is not None:
                scan_dt = scan_dt.tz_localize(df.index.tz)
            after = df[df.index > scan_dt]
            if len(after) < 1:
                p["earnings_gap"] = False
                continue

            scan_day = df[df.index <= scan_dt]
            check = after.iloc[:min(10, len(after))]
            found = False
            for i in range(len(check)):
                if i == 0:
                    prev_c = float(scan_day.iloc[-1]["Close"]) if len(scan_day) > 0 else None
                else:
                    prev_c = float(check.iloc[i - 1]["Close"])
                if prev_c and prev_c > 0:
                    gap = abs(float(check.iloc[i]["Open"]) - prev_c) / prev_c
                    if gap >= 0.05:
                        found = True
                        break
            p["earnings_gap"] = found
            if found:
                flagged += 1

    return flagged


def bootstrap_running_stats() -> bool:
    """One-time migration: convert existing scored picks into running stats.
    Applies per-pick time decay to preserve historical weighting.
    Returns True if bootstrap was performed."""
    state = _load_adaptive_state()
    if "running_stats" in state:
        return False  # already bootstrapped

    rs = _init_running_stats()

    if PICKS_HISTORY_FILE.exists():
        try:
            history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = {"picks": []}

        consumed = []
        still_active = []
        all_picks = history.get("picks", [])

        # Also pull in archived picks (winners.json / losers.json) so a
        # re-bootstrap reconstructs the full history, not just active picks.
        archive_data = {}  # path -> list, for writing back flagged picks
        for path in (WINNERS_JSON, LOSERS_JSON):
            if path.exists():
                try:
                    archived = json.loads(path.read_text(encoding="utf-8"))
                    archive_data[path] = archived
                    for p in archived:
                        p["_from_archive"] = True
                    all_picks.extend(archived)
                except Exception:
                    pass

        # Retroactively flag earnings gaps in archived picks
        n_flagged = _scan_for_earnings_gaps(all_picks)
        if n_flagged:
            # Persist earnings_gap flags back to archive files (clean temp flags first)
            for path, entries in archive_data.items():
                for e in entries:
                    e.pop("_from_archive", None)
                path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
                for e in entries:
                    e["_from_archive"] = True  # restore for downstream filtering

        for p in all_picks:
            is_day = p.get("trade_type", "").startswith("DAY TRADE")
            result_key = "result_1d" if is_day else "result_10d"
            result = p.get(result_key)
            if result in ("win", "loss"):
                # Earnings gap: consume but don't ingest
                if p.get("earnings_gap"):
                    p["consumed"] = True
                    consumed.append(p)
                    continue
                tier = p.get("tier", "C")
                if tier == "C":
                    rs["c_tier_count"] = rs.get("c_tier_count", 0) + 1
                    if p.get("score", 0) >= 5.0:
                        rs["c_tier_near_b_count"] = rs.get("c_tier_near_b_count", 0) + 1
                else:
                    tw = TIER_ADAPTIVE_WEIGHTS.get(tier, 0.01)
                    td = _time_decay_weight(p.get("scan_date", ""))
                    _ingest_pick(rs, p, tw * td)
                p["consumed"] = True
                consumed.append(p)
            elif result == "draw":
                # Draws: consume (remove from active) but don't feed into stats
                p["consumed"] = True
                consumed.append(p)
            else:
                still_active.append(p)

        # Save trimmed picks_history — only active picks remain (no archive leaks)
        history["picks"] = [p for p in still_active if not p.get("_from_archive")]
        PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
        # Only dump newly consumed picks — archived ones are already in audit files
        new_consumed = [p for p in consumed if not p.pop("_from_archive", False)]
        if new_consumed:
            _dump_to_audit(new_consumed)

    rs["last_consumed"] = date.today().isoformat()
    state["running_stats"] = rs
    _save_adaptive_state(state)
    return True


def _migrate_w_count(rs: dict):
    """One-time migration: add w_count field to all buckets that have w_total.
    Initializes w_count = w_total as the best available approximation."""
    if rs["total"].get("w_count") is not None:
        return  # already migrated
    # total
    rs["total"]["w_count"] = rs["total"].get("w_total", 0)
    # signals, play_types, score_buckets, rs_buckets, sector, regime_total, adx_buckets
    for bucket_name in ("signals", "play_types", "score_buckets", "rs_buckets",
                        "sector", "regime_total", "adx_buckets",
                        "si_buckets", "float_buckets", "analyst_buckets",
                        "inst_buckets", "beta_buckets", "factors"):
        for bucket_data in rs.get(bucket_name, {}).values():
            if "w_total" in bucket_data and "w_count" not in bucket_data:
                bucket_data["w_count"] = bucket_data["w_total"]
    # regime_signals (nested)
    for regime_data in rs.get("regime_signals", {}).values():
        for sig_data in regime_data.values():
            if "w_total" in sig_data and "w_count" not in sig_data:
                sig_data["w_count"] = sig_data["w_total"]
    # stop (uses w_wins + w_losses, not w_total)
    if "w_count" not in rs.get("stop", {}):
        stop = rs.get("stop", {})
        stop["w_count"] = stop.get("w_wins", 0) + stop.get("w_losses", 0)
    # regime_stop (same structure as stop — w_wins + w_losses)
    for rsb in rs.get("regime_stop", {}).values():
        if "w_count" not in rsb:
            rsb["w_count"] = rsb.get("w_wins", 0) + rsb.get("w_losses", 0)


def _migrate_factors(rs: dict):
    """One-time migration: bootstrap factor buckets from existing signal data."""
    if rs.get("factors"):
        return  # already have factor data
    rs["factors"] = {}
    for sig_name, sig_data in rs.get("signals", {}).items():
        factor = SIGNAL_TO_FACTOR.get(sig_name, "context")
        fb = rs["factors"].setdefault(
            factor, {"w_wins": 0.0, "w_total": 0.0, "count": 0, "wins": 0, "w_count": 0})
        fb["w_wins"] += sig_data.get("w_wins", 0)
        fb["w_total"] += sig_data.get("w_total", 0)
        fb["w_count"] += sig_data.get("w_count", sig_data.get("w_total", 0))
        fb["count"] += sig_data.get("count", 0)
        fb["wins"] += sig_data.get("wins", 0)


def consume_scored_picks() -> int:
    """Fold newly scored picks into running stats, then archive them.
    Called after score_previous_picks + log_trade_results + analyze_losers."""
    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if rs is None:
        # Should not happen (bootstrap runs first), but handle gracefully
        rs = _init_running_stats()

    # Migration: add w_count to existing buckets that lack it
    _migrate_w_count(rs)
    # Migration: bootstrap factor buckets from existing signal data
    _migrate_factors(rs)

    # Bulk decay existing stats
    last_consumed = rs.get("last_consumed")
    if last_consumed:
        try:
            days_since = (date.today() - date.fromisoformat(last_consumed)).days
        except Exception:
            days_since = 1
        if days_since > 0:
            decay = math.exp(-0.693 * days_since / 60)
            _decay_running_stats(rs, decay)

    # Load picks
    if not PICKS_HISTORY_FILE.exists():
        return 0
    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0

    picks = history.get("picks", [])

    # Find newly scored picks
    newly_consumed = []
    still_active = []
    for p in picks:
        is_day = p.get("trade_type", "").startswith("DAY TRADE")
        result_key = "result_1d" if is_day else "result_10d"
        result = p.get(result_key)
        if result in ("win", "loss"):
            # Earnings gap: consume but don't ingest (binary event, not setup failure)
            if p.get("earnings_gap"):
                newly_consumed.append(p)
                continue
            tier = p.get("tier", "C")
            if tier == "C":
                rs["c_tier_count"] = rs.get("c_tier_count", 0) + 1
                if p.get("score", 0) >= 5.0:
                    rs["c_tier_near_b_count"] = rs.get("c_tier_near_b_count", 0) + 1
            else:
                tw = TIER_ADAPTIVE_WEIGHTS.get(tier, 0.01)
                _ingest_pick(rs, p, tw)
            newly_consumed.append(p)
        elif result == "draw":
            # Draws: consume (remove from active) but don't feed into stats
            newly_consumed.append(p)
        else:
            still_active.append(p)

    if not newly_consumed:
        # Still save — decay was applied
        rs["last_consumed"] = date.today().isoformat()
        state["running_stats"] = rs
        _save_adaptive_state(state)
        return 0

    # Consumed picks are gone — analyze_losers() already ran before us
    history["picks"] = still_active
    PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    _dump_to_audit(newly_consumed)

    rs["last_consumed"] = date.today().isoformat()
    state["running_stats"] = rs
    _save_adaptive_state(state)

    return len(newly_consumed)


# ---------------------------------------------------------------------------
# Adaptive functions — read from running_stats
# ---------------------------------------------------------------------------

def get_adaptive_weights() -> dict:
    """Signal weights adjusted by historical win rates.
    Signals that historically win get boosted, losers get penalized.
    Returns base weights if insufficient data (cold start)."""
    adaptive = dict(SIGNAL_WEIGHTS)

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs or rs["total"]["count"] < 10:
        return adaptive

    total = rs["total"]
    if total["w_total"] == 0:
        return adaptive
    baseline_wr = total["w_wins"] / total["w_total"]

    # Early penalty for clearly toxic signals (before 10-pick confidence threshold)
    for signal_name, base_w in SIGNAL_WEIGHTS.items():
        sig = rs.get("signals", {}).get(signal_name)
        if not sig:
            continue
        if sig.get("count", 0) >= 5 and sig.get("wins", 0) == 0:
            adaptive[signal_name] = round(base_w * 0.75, 3)

    pa = max(0.1, baseline_wr * 4)
    pb = max(0.1, (1 - baseline_wr) * 4)
    for signal_name, base_w in SIGNAL_WEIGHTS.items():
        sig = rs.get("signals", {}).get(signal_name)
        if not sig or sig.get("w_total", 0) <= 0:
            continue
        posterior_mean, conf = _beta_confidence(sig["w_wins"], sig["w_total"], pa, pb)
        delta = (posterior_mean - baseline_wr) * 2.0
        delta = max(-0.50, min(0.50, delta))
        dampening = SIGNAL_DAMPENING.get(signal_name, 1.0)
        adjustment = delta * conf * dampening
        adaptive[signal_name] = round(base_w * (1.0 + adjustment), 3)
        adaptive[signal_name] = max(base_w * 0.50, min(base_w * 1.50, adaptive[signal_name]))

    # Track individual signal weight adjustments in health history
    history = state.setdefault("history", {})
    any_tracked = False
    for sig, val in adaptive.items():
        base = SIGNAL_WEIGHTS.get(sig)
        if base is not None and abs(val - base) > base * 0.005:
            hkey = f"signal.{sig}"
            h = history.get(hkey, [])
            h.append(round(val, 4))
            history[hkey] = h[-10:]
            any_tracked = True
    if any_tracked:
        _save_adaptive_state(state)

    return adaptive


def get_adaptive_factor_weights():
    """Learn factor-level weights from outcomes (7 factors vs 33 signals).
    Returns dict of factor_name -> adjusted weight."""
    state = _load_adaptive_state()
    rs = state.get("running_stats")
    adaptive = dict(FACTOR_WEIGHTS)

    if not rs or rs["total"]["count"] < 10:
        return adaptive

    total = rs["total"]
    if total["w_total"] <= 0:
        return adaptive
    baseline_wr = total["w_wins"] / total["w_total"]

    pa = max(0.1, baseline_wr * 4)
    pb = max(0.1, (1 - baseline_wr) * 4)
    factors = rs.get("factors", {})
    for factor_name, base_w in FACTOR_WEIGHTS.items():
        fdata = factors.get(factor_name)
        if not fdata or fdata.get("w_total", 0) <= 0:
            continue
        posterior_mean, conf = _beta_confidence(fdata["w_wins"], fdata["w_total"], pa, pb)
        delta = (posterior_mean - baseline_wr) * 2.0
        delta = max(-0.50, min(0.50, delta))
        adjustment = delta * conf
        adaptive[factor_name] = round(base_w * (1.0 + adjustment), 3)
        adaptive[factor_name] = max(base_w * 0.50, min(base_w * 1.50, adaptive[factor_name]))

    return adaptive


def get_adaptive_pillars() -> dict:
    """Learn which scoring pillars actually predict winners.
    Compares avg pillar values in winning vs losing picks.
    Returns multipliers (0.7 - 1.3) for each pillar, normalized to keep max=10.
    Dampened between runs to prevent seesaw."""
    def _finish(result):
        state = _load_adaptive_state()
        prev = state.get("pillar_mults", {})
        if prev:
            result = _damp_dict(result, prev)
            total_w = sum(PILLAR_DEFAULTS[k] * result.get(k, 1.0) for k in PILLAR_DEFAULTS)
            if total_w > 0:
                scale = 10.0 / total_w
                for k in result:
                    result[k] = round(result[k] * scale, 3)
        state["pillar_mults"] = dict(result)
        history = state.setdefault("history", {})
        for k, v in result.items():
            if isinstance(v, (int, float)):
                hkey = f"pillar.{k}"
                h = history.get(hkey, [])
                h.append(round(v, 4))
                history[hkey] = h[-10:]
        _save_adaptive_state(state)
        return result

    pillars = {k: 1.0 for k in PILLAR_DEFAULTS}

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs or rs.get("win_count", 0) < 10 or rs.get("loss_count", 0) < 10:
        return _finish(pillars)

    for pillar in PILLAR_DEFAULTS:
        w_data = rs.get("pillars_win", {}).get(pillar)
        l_data = rs.get("pillars_loss", {}).get(pillar)
        if not w_data or not l_data or w_data["w_weight"] == 0 or l_data["w_weight"] == 0:
            continue
        win_avg = w_data["w_sum"] / w_data["w_weight"]
        loss_avg = l_data["w_sum"] / l_data["w_weight"]
        if loss_avg == 0:
            continue

        ratio = win_avg / loss_avg
        mult = 0.7 + 0.6 * max(0.0, min(1.0, (ratio - 0.5)))
        # Beta-like confidence from effective sample weight (prior strength = 4)
        eff_sample = min(w_data["w_weight"], l_data["w_weight"])
        conf = eff_sample / (eff_sample + 4.0) if eff_sample > 0 else 0.0
        pillars[pillar] = round(1.0 + (mult - 1.0) * conf, 3)

    # Renormalize so total max stays at 10.0
    total_weighted = sum(PILLAR_DEFAULTS[k] * pillars[k] for k in PILLAR_DEFAULTS)
    if total_weighted > 0:
        scale = 10.0 / total_weighted
        for k in pillars:
            pillars[k] = round(pillars[k] * scale, 3)

    return _finish(pillars)


def get_adaptive_win_rates() -> dict:
    """Build empirical win rate curve from historical picks by score bucket.
    Returns dict of {score_bucket: {"empirical": rate, "count": n, "blended": rate}}.
    Blends empirical data with sigmoid prior — data takes over as sample grows.
    Dampened between runs to prevent seesaw."""
    def _finish(result):
        state = _load_adaptive_state()
        prev = state.get("win_rate_curve", {})
        if prev:
            for k in result:
                k_str = str(k)
                if k_str in prev and isinstance(prev[k_str], (int, float)):
                    result[k]["blended"] = round(
                        _damp_value(result[k]["blended"], prev[k_str]), 3
                    )
        state["win_rate_curve"] = {str(k): v["blended"] for k, v in result.items()}
        # Fit sigmoid params from blended rates and save (EMA-dampened)
        fitted = _fit_sigmoid_params(result)
        prev_sig = state.get("sigmoid_params", dict(DEFAULT_SIGMOID_PARAMS))
        dampened = {}
        for k in fitted:
            dampened[k] = round(_damp_value(fitted[k], prev_sig.get(k, fitted[k])), 4)
        state["sigmoid_params"] = dampened
        _module_cache["sigmoid"] = dampened  # update in-process cache
        history = state.setdefault("history", {})
        rates = [v["blended"] for v in result.values() if isinstance(v.get("blended"), (int, float))]
        if rates:
            h = history.get("winrate_mean", [])
            h.append(round(sum(rates) / len(rates), 4))
            history["winrate_mean"] = h[-10:]
        for bucket_score, data in result.items():
            if isinstance(data, dict) and data.get("count", 0) >= 3:
                hkey = f"winrate_{bucket_score}"
                h = history.get(hkey, [])
                h.append(round(data["blended"], 4))
                history[hkey] = h[-10:]
        _save_adaptive_state(state)
        return result

    # Use calibrated sigmoid params for priors (previous run's fit, or defaults)
    sig_p = get_sigmoid_params()
    buckets = {}
    for s in [x * 0.5 for x in range(8, 21)]:  # 4.0 to 10.0
        sigmoid = 1.0 / (1.0 + math.exp(-sig_p["slope"] * (s - sig_p["midpoint"])))
        prior = sig_p["floor"] + sigmoid * (sig_p["ceiling"] - sig_p["floor"])
        buckets[s] = {"empirical": None, "count": 0, "blended": prior, "prior": prior}

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs:
        return _finish(buckets)

    # Populate from running stats
    for sb_key, sb_data in rs.get("score_buckets", {}).items():
        try:
            b = float(sb_key)
        except ValueError:
            continue
        if b not in buckets:
            continue
        n = sb_data["count"]
        if n == 0 or sb_data["w_total"] == 0:
            continue
        emp = sb_data["w_wins"] / sb_data["w_total"]
        buckets[b]["empirical"] = round(emp, 3)
        buckets[b]["count"] = n
        # Beta-Binomial: prior encodes the sigmoid expectation for this bucket
        prior_rate = buckets[b]["prior"]
        pa = max(0.1, prior_rate * 4)
        pb_val = max(0.1, (1 - prior_rate) * 4)
        posterior_mean, _ = _beta_confidence(sb_data["w_wins"], sb_data["w_total"], pa, pb_val)
        buckets[b]["blended"] = round(max(0.15, min(0.75, posterior_mean)), 3)

    # Smooth: fill sparse buckets from neighbors
    sorted_keys = sorted(buckets.keys())
    smoothed = {}
    for i, k in enumerate(sorted_keys):
        if buckets[k]["count"] >= 5:
            smoothed[k] = buckets[k]["blended"]
            continue
        vals = [(buckets[k]["blended"], max(1, buckets[k]["count"]))]
        if i > 0:
            prev = sorted_keys[i - 1]
            vals.append((buckets[prev]["blended"], max(1, buckets[prev]["count"])))
        if i < len(sorted_keys) - 1:
            nxt = sorted_keys[i + 1]
            vals.append((buckets[nxt]["blended"], max(1, buckets[nxt]["count"])))
        total_w = sum(w for _, w in vals)
        smoothed[k] = round(sum(v * w for v, w in vals) / total_w, 3) if total_w > 0 else buckets[k]["prior"]

    for k in smoothed:
        buckets[k]["blended"] = smoothed[k]

    return _finish(buckets)


def _get_adaptive_rs_adjustment(rs_raw: float) -> float:
    """Learn actual win rate impact of RS values from history.
    Falls back to hardcoded tiers if insufficient data."""
    state = _load_adaptive_state()
    rs = state.get("running_stats")

    if not rs or rs.get("rs_total_count", 0) < 20:
        # Fallback: original hardcoded adjustments
        if rs_raw > 10:
            return 0.04
        elif rs_raw > 5:
            return 0.02
        elif rs_raw < -10:
            return -0.04
        elif rs_raw < -5:
            return -0.02
        return 0.0

    bucket_name = _classify_rs_bucket(rs_raw)
    if bucket_name == "neutral":
        return 0.0

    # Neutral baseline
    neutral = rs.get("rs_buckets", {}).get("neutral", {})
    n_total = neutral.get("w_total", 0)
    baseline = neutral["w_wins"] / n_total if n_total > 0 else 0.5

    bucket = rs.get("rs_buckets", {}).get(bucket_name, {})
    if bucket.get("w_total", 0) <= 0:
        defaults = {"strong_leader": 0.04, "leader": 0.02, "laggard": -0.02, "strong_laggard": -0.04}
        return defaults.get(bucket_name, 0.0)

    # Beta-Binomial with weak uniform prior (alpha=1, beta=1)
    posterior_mean, conf = _beta_confidence(bucket["w_wins"], bucket["w_total"], 1.0, 1.0)
    adj = posterior_mean - baseline
    adj = max(-0.05, min(0.05, adj))
    return round(adj * conf, 4)


def _get_adaptive_sector_adjustment(sector_status: str,
                                     setup_context: str = None) -> float:
    """Learn actual win rate impact of sector rotation from history.
    Falls back to ±3% if insufficient data.
    Play-type aware: counter_trend setups flip the logic."""
    if sector_status is None:
        return 0.0

    is_reversal = setup_context == "counter_trend"

    state = _load_adaptive_state()
    rs = state.get("running_stats")

    if not rs or rs.get("sector_total_count", 0) < 20:
        base = 0.0
        if sector_status == "leading":
            base = 0.03
        elif sector_status == "lagging":
            base = -0.03
        return -base if is_reversal else base

    # Neutral baseline
    neutral = rs.get("sector", {}).get("neutral", {})
    n_total = neutral.get("w_total", 0)
    baseline = neutral["w_wins"] / n_total if n_total > 0 else 0.5

    target = rs.get("sector", {}).get(sector_status, {})
    if target.get("w_total", 0) <= 0:
        base = 0.0
        if sector_status == "leading":
            base = 0.03
        elif sector_status == "lagging":
            base = -0.03
        return -base if is_reversal else base

    # Beta-Binomial with weak uniform prior (alpha=1, beta=1)
    posterior_mean, conf = _beta_confidence(target["w_wins"], target["w_total"], 1.0, 1.0)
    adj = posterior_mean - baseline
    adj = max(-0.05, min(0.05, adj))
    learned = round(adj * conf, 4)
    return -learned if is_reversal else learned


def get_regime_signal_weights(regime: str) -> dict:
    """Learn which signals work best in each regime (chop/range/trend).
    Returns adjusted weights for the current regime, dampened between runs."""
    def _finish(result):
        state = _load_adaptive_state()
        key = f"regime_weights_{regime}"
        prev = state.get(key, {})
        if prev:
            result = _damp_dict(result, prev)
        state[key] = {k: v for k, v in result.items() if isinstance(v, (int, float))}
        history = state.setdefault("history", {})
        deltas = [abs(result[s] - SIGNAL_WEIGHTS[s]) for s in result
                  if s in SIGNAL_WEIGHTS and isinstance(result[s], (int, float))]
        if deltas:
            hkey = f"regime_drift_{regime}"
            h = history.get(hkey, [])
            h.append(round(sum(deltas) / len(deltas), 4))
            history[hkey] = h[-10:]
        _save_adaptive_state(state)
        return result

    adaptive = get_adaptive_weights()  # start with overall adaptive weights

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs:
        return _finish(adaptive)

    regime_data = rs.get("regime_total", {}).get(regime, {})
    if regime_data.get("count", 0) < 15:
        return _finish(adaptive)

    # Baseline win rate for this regime
    if regime_data.get("w_total", 0) == 0:
        return _finish(adaptive)
    baseline_wr = regime_data["w_wins"] / regime_data["w_total"]

    # Per-signal win rate within this regime (Beta-Binomial confidence)
    pa = max(0.1, baseline_wr * 4)
    pb = max(0.1, (1 - baseline_wr) * 4)
    regime_sigs = rs.get("regime_signals", {}).get(regime, {})
    for sig, sig_data in regime_sigs.items():
        if sig_data.get("w_total", 0) <= 0:
            continue
        posterior_mean, conf = _beta_confidence(sig_data["w_wins"], sig_data["w_total"], pa, pb)
        delta = (posterior_mean - baseline_wr) * 2.0
        delta = max(-0.50, min(0.50, delta))
        dampening = SIGNAL_DAMPENING.get(sig, 1.0)
        base_w = adaptive.get(sig, SIGNAL_WEIGHTS.get(sig, 0.5))
        adjusted = base_w * (1.0 + delta * conf * dampening)
        orig_base = SIGNAL_WEIGHTS.get(sig, 0.5)
        adaptive[sig] = round(max(orig_base * 0.50, min(orig_base * 1.50, adjusted)), 3)

    return _finish(adaptive)


def _stop_mult_from_data(w_wins, w_losses):
    """Compute stop multiplier from TP/stop hit data using Beta-Binomial."""
    w_total = w_wins + w_losses
    if w_total <= 0:
        return 1.5, 0.0  # default, zero confidence
    posterior_mean, conf = _beta_confidence(w_wins, w_total, 2.0, 2.0)
    stop_rate = 1 - posterior_mean
    mult = 1.2 + (stop_rate - 0.3) * 2.0
    mult = max(1.0, min(2.5, mult))
    result = 1.5 + (mult - 1.5) * conf
    return round(max(1.0, min(2.5, result)), 2), conf


def get_adaptive_stop_multiplier(regime: str = None) -> float:
    """Learn optimal ATR stop multiplier from history.
    Tries regime-specific data first (needs 15+ picks), falls back to global.
    Returns ATR multiplier (default 1.5, range 1.0-2.5). Dampened between runs."""
    def _finish(result):
        state = _load_adaptive_state()
        prev = state.get("stop_multiplier")
        if prev is not None and isinstance(prev, (int, float)):
            result = round(_damp_value(result, prev), 2)
        state["stop_multiplier"] = result
        history = state.setdefault("history", {})
        h = history.get("stop_multiplier", [])
        h.append(round(result, 4))
        history["stop_multiplier"] = h[-10:]
        _save_adaptive_state(state)
        return result

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs:
        return _finish(1.5)

    # Try regime-specific stop data first
    if regime:
        regime_stop = rs.get("regime_stop", {}).get(regime, {})
        if regime_stop.get("count", 0) >= 15:
            rw = regime_stop.get("w_wins", 0)
            rl = regime_stop.get("w_losses", 0)
            result, conf = _stop_mult_from_data(rw, rl)
            if conf > 0.2:
                return _finish(result)

    # Global fallback
    result, _ = _stop_mult_from_data(
        rs["stop"].get("w_wins", 0), rs["stop"].get("w_losses", 0)
    )
    return _finish(result)


def get_adaptive_regime_thresholds() -> dict:
    """Learn optimal ADX thresholds for chop/range/trend classification.
    Returns {"chop_threshold": float, "trend_threshold": float}.
    Defaults: chop < 18, trend > 25."""
    defaults = {"chop_threshold": 18.0, "trend_threshold": 25.0}

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs or rs.get("adx_total_count", 0) < 30:
        return defaults

    # Win rate per ADX bucket (need 5+ entries)
    bucket_wr = {}
    for b_key, b_data in rs.get("adx_buckets", {}).items():
        try:
            b = int(b_key)
        except ValueError:
            continue
        if b_data["count"] >= 5 and b_data["w_total"] > 0:
            bucket_wr[b] = b_data["w_wins"] / b_data["w_total"]

    if len(bucket_wr) < 3:
        return defaults

    # --- Chop threshold ---
    chop_zone_wr = [wr for b, wr in bucket_wr.items() if 12 <= b < 18]
    above_chop_wr = [wr for b, wr in bucket_wr.items() if 18 <= b < 24]

    chop_threshold = 18.0
    if chop_zone_wr and above_chop_wr:
        cz = sum(chop_zone_wr) / len(chop_zone_wr)
        az = sum(above_chop_wr) / len(above_chop_wr)
        gap = az - cz

        if gap < 0.02:
            chop_threshold = 14.0
        elif gap < 0.05:
            chop_threshold = 15.0
        elif gap < 0.08:
            chop_threshold = 16.0
        elif gap > 0.15:
            chop_threshold = 20.0

    # --- Trend threshold ---
    range_zone_wr = [wr for b, wr in bucket_wr.items() if 18 <= b < 25]
    trend_zone_wr = [wr for b, wr in bucket_wr.items() if b >= 25]

    trend_threshold = 25.0
    if range_zone_wr and trend_zone_wr:
        rz = sum(range_zone_wr) / len(range_zone_wr)
        tz = sum(trend_zone_wr) / len(trend_zone_wr)
        gap = tz - rz

        if gap < 0.02:
            trend_threshold = 28.0
        elif gap < 0.05:
            trend_threshold = 26.0
        elif gap > 0.15:
            trend_threshold = 22.0

    # Dampen: move 30% toward new value from previous
    prev_chop = state.get("regime_chop_threshold", 18.0)
    prev_trend = state.get("regime_trend_threshold", 25.0)

    chop_threshold = round(prev_chop + 0.3 * (chop_threshold - prev_chop), 1)
    trend_threshold = round(prev_trend + 0.3 * (trend_threshold - prev_trend), 1)

    # Clamp to sane ranges
    chop_threshold = max(12.0, min(22.0, chop_threshold))
    trend_threshold = max(20.0, min(30.0, trend_threshold))

    # Ensure minimum gap of 4 between thresholds
    if trend_threshold - chop_threshold < 4.0:
        mid = (chop_threshold + trend_threshold) / 2
        chop_threshold = round(mid - 2.0, 1)
        trend_threshold = round(mid + 2.0, 1)

    # Save and track in health history
    state["regime_chop_threshold"] = chop_threshold
    state["regime_trend_threshold"] = trend_threshold

    history = state.setdefault("history", {})
    for key, val in [("regime_chop", chop_threshold), ("regime_trend", trend_threshold)]:
        h = history.get(key, [])
        h.append(val)
        history[key] = h[-10:]

    _save_adaptive_state(state)

    return {"chop_threshold": chop_threshold, "trend_threshold": trend_threshold}


def get_adaptive_distrib_threshold() -> float:
    """Learn optimal distribution volume threshold from pick outcomes.
    Returns the buy/sell ratio below which we classify as distribution.
    Default: 0.77 (sellers must dominate by 30%+). Adaptive range: 0.60-0.95."""
    default = 0.77

    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs:
        return default

    buckets = rs.get("distrib_vol_buckets", {})
    total_picks = sum(b.get("count", 0) for b in buckets.values())
    if total_picks < 15:
        return state.get("distrib_vol_threshold", default)

    # Try candidate thresholds from 0.60 to 0.95 in steps of 0.05
    # Find the one that maximizes win rate for picks BELOW the threshold
    # (strong distribution confirmed) vs picks ABOVE (weak/no distribution)
    best_threshold = default
    best_gap = -1.0

    for candidate in [x / 100 for x in range(60, 96, 5)]:
        below_w, below_t = 0, 0
        above_w, above_t = 0, 0
        for b_key, b_data in buckets.items():
            try:
                ratio = float(b_key)
            except ValueError:
                continue
            count = b_data.get("count", 0)
            wins = b_data.get("wins", 0)
            if ratio < candidate:
                below_w += wins
                below_t += count
            else:
                above_w += wins
                above_t += count

        if below_t >= 3 and above_t >= 3:
            below_wr = below_w / below_t
            above_wr = above_w / above_t
            gap = below_wr - above_wr  # positive = distribution signal is predictive
            if gap > best_gap:
                best_gap = gap
                best_threshold = candidate

    # EMA damp: move 30% toward new value
    prev = state.get("distrib_vol_threshold", default)
    threshold = round(prev + 0.3 * (best_threshold - prev), 3)

    # Clamp to sane range
    threshold = max(0.60, min(0.95, threshold))

    # Save and track
    state["distrib_vol_threshold"] = threshold
    history = state.setdefault("history", {})
    h = history.get("distrib_vol", [])
    h.append(threshold)
    history["distrib_vol"] = h[-10:]
    _save_adaptive_state(state)

    return threshold


def track_rs_and_sector_health():
    """Compute RS bucket and sector adjustment values and track in health history.
    Called once per scan — provides visibility into RS and sector learning."""
    state = _load_adaptive_state()
    rs = state.get("running_stats")
    if not rs:
        return

    history = state.setdefault("history", {})
    tracked = False

    # --- RS bucket win rate adjustments ---
    if rs.get("rs_total_count", 0) >= 20:
        neutral = rs.get("rs_buckets", {}).get("neutral", {})
        n_weight = neutral.get("w_total", 0)
        baseline = neutral["w_wins"] / n_weight if n_weight > 0 else 0.5

        for bname, bucket in rs.get("rs_buckets", {}).items():
            if bucket.get("count", 0) >= 5 and bucket.get("w_total", 0) > 0:
                bucket_wr = bucket["w_wins"] / bucket["w_total"]
                adj = round(bucket_wr - baseline, 4)
                hkey = f"rs.{bname}"
                h = history.get(hkey, [])
                h.append(adj)
                history[hkey] = h[-10:]
                tracked = True

    # --- Sector rotation adjustments ---
    if rs.get("sector_total_count", 0) >= 20:
        neutral = rs.get("sector", {}).get("neutral", {})
        n_weight = neutral.get("w_total", 0)
        baseline = neutral["w_wins"] / n_weight if n_weight > 0 else 0.5

        for gname, group in rs.get("sector", {}).items():
            if group.get("count", 0) >= 5 and group.get("w_total", 0) > 0:
                group_wr = group["w_wins"] / group["w_total"]
                adj = round(group_wr - baseline, 4)
                hkey = f"sector.{gname}"
                h = history.get(hkey, [])
                h.append(adj)
                history[hkey] = h[-10:]
                tracked = True

    if tracked:
        _save_adaptive_state(state)


# ---------------------------------------------------------------------------
# Adaptive health (unchanged — reads from history trail, not picks)
# ---------------------------------------------------------------------------

def get_adaptive_health() -> dict:
    """Analyze adaptive learning stability from run history.
    Counts direction reversals to detect seesaw."""
    state = _load_adaptive_state()
    history = state.get("history", {})

    if not history:
        return {"has_data": False, "total_tracked": 0, "oscillating": [], "metrics": {}}

    metrics = {}
    for key, values in history.items():
        if not isinstance(values, list) or len(values) < 3:
            continue

        reversals = 0
        for i in range(2, len(values)):
            d1 = values[i - 1] - values[i - 2]
            d2 = values[i] - values[i - 1]
            if d1 != 0 and d2 != 0 and d1 * d2 < 0:
                reversals += 1

        max_reversals = len(values) - 2
        reversal_ratio = reversals / max_reversals if max_reversals > 0 else 0

        changes = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
        if len(changes) >= 4:
            mid = len(changes) // 2
            early_avg = sum(changes[:mid]) / mid
            late_avg = sum(changes[mid:]) / (len(changes) - mid)
            converging = early_avg > 0.0001 and late_avg < early_avg * 0.7
        else:
            converging = False

        if reversal_ratio > 0.6:
            status = "seesaw"
        elif converging:
            status = "converging"
        elif len(changes) >= 3 and all(c < 0.001 for c in changes[-3:]):
            status = "settled"
        else:
            status = "stable"

        metrics[key] = {
            "current": values[-1],
            "runs": len(values),
            "reversals": reversals,
            "reversal_ratio": round(reversal_ratio, 2),
            "status": status,
        }

    oscillating = [k for k, m in metrics.items() if m["status"] == "seesaw"]

    return {
        "has_data": bool(metrics),
        "total_tracked": len(metrics),
        "oscillating_count": len(oscillating),
        "oscillating": oscillating,
        "metrics": metrics,
    }


def write_metrics_report():
    """Write metrics report — win rates across all dimensions,
    sourced from running_stats (populated at consumption time)."""
    state = _load_adaptive_state()
    rs = state.get("running_stats")

    L = []
    L.append("=" * 64)
    L.append("  ADAPTIVE METRICS")
    L.append(f"  {_datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    L.append("=" * 64)

    if not rs or rs.get("total", {}).get("count", 0) == 0:
        L.append("")
        L.append("  No consumed picks yet — metrics will appear after trades are scored.")
        L.append("")
        _write_metrics_file(L)
        return

    total_count = rs["total"]["count"]
    total_w = rs["total"]["w_total"]
    total_ww = rs["total"]["w_wins"]
    overall_wr = (total_ww / total_w * 100) if total_w > 0 else 0

    # Derive total wins/losses from tier buckets (raw counts)
    tiers = rs.get("tiers", {})
    all_wins = sum(t.get("wins", 0) for t in tiers.values())
    all_losses = sum(t.get("losses", 0) for t in tiers.values())
    draws_approx = total_count - all_wins - all_losses

    near_misses = rs.get("near_miss_count", 0)
    L.append(f"  {total_count} consumed  |  {all_wins}W {all_losses}L"
             + (f" {draws_approx}D" if draws_approx > 0 else "")
             + (f" ({near_misses} near-miss)" if near_misses > 0 else "")
             + f"  |  {overall_wr:.0f}% weighted win rate")
    L.append("")

    def _wr(w, l, pad=0):
        """Format W/L with win%."""
        t = w + l
        if t == 0:
            return f"{'':>{pad}}---"
        pct = w / t * 100
        return f"{w}W / {l}L{' ':>1}{pct:>5.0f}%"

    # ------------------------------------------------------------------
    #  TIERS
    # ------------------------------------------------------------------
    L.append("=" * 64)
    L.append("  TIERS")
    L.append("=" * 64)
    td = rs.get("tier_direction", {})
    ttt = rs.get("tier_trade_type", {})
    for tier in ("A", "B"):
        tb = tiers.get(tier, {})
        tw, tl = tb.get("wins", 0), tb.get("losses", 0)
        if tw + tl == 0:
            L.append(f"  {tier}-TIER{' ':>25}---")
            continue
        L.append(f"  {tier}-TIER{' ':>25}{_wr(tw, tl)}")
        # direction breakdown
        for d in ("bullish", "bearish"):
            db = td.get(f"{tier}_{d}", {})
            dw, dl = db.get("wins", 0), db.get("losses", 0)
            if dw + dl > 0:
                label = "Bull" if d == "bullish" else "Bear"
                L.append(f"    {label:>8}{' ':>22}{_wr(dw, dl)}")
        # trade type breakdown
        for tt_name in ("day", "swing"):
            ttb = ttt.get(f"{tier}_{tt_name}", {})
            tw2, tl2 = ttb.get("wins", 0), ttb.get("losses", 0)
            if tw2 + tl2 > 0:
                label = "Day" if tt_name == "day" else "Swing"
                L.append(f"    {label:>8}{' ':>22}{_wr(tw2, tl2)}")
    # C-tier summary — tracked but not scored
    c_count = rs.get("c_tier_count", 0)
    c_near = rs.get("c_tier_near_b_count", 0)
    if c_count > 0:
        L.append(f"  C-TIER{' ':>23}{c_count} tracked ({c_near} near B-tier)")
    L.append("")

    # ------------------------------------------------------------------
    #  DIRECTION + TRADE TYPE totals
    # ------------------------------------------------------------------
    dirs = rs.get("directions", {})
    tts = rs.get("trade_types", {})
    bull = dirs.get("bullish", {})
    bear = dirs.get("bearish", {})
    day = tts.get("day", {})
    swing = tts.get("swing", {})
    bw, bl = bull.get("wins", 0), bull.get("losses", 0)
    ew, el = bear.get("wins", 0), bear.get("losses", 0)
    dw, dl = day.get("wins", 0), day.get("losses", 0)
    sw, sl = swing.get("wins", 0), swing.get("losses", 0)
    if bw + bl + ew + el > 0:
        L.append(f"  {'Overall Bull':<30}{_wr(bw, bl)}")
        L.append(f"  {'Overall Bear':<30}{_wr(ew, el)}")
    if dw + dl + sw + sl > 0:
        L.append(f"  {'Overall Day':<30}{_wr(dw, dl)}")
        L.append(f"  {'Overall Swing':<30}{_wr(sw, sl)}")
    L.append("")

    # ------------------------------------------------------------------
    #  PILLARS (avg contribution for wins vs losses)
    # ------------------------------------------------------------------
    pw = rs.get("pillars_win", {})
    pl = rs.get("pillars_loss", {})
    if pw or pl:
        L.append("=" * 64)
        L.append("  PILLARS (avg score)")
        L.append("=" * 64)
        L.append(f"  {'':>22}{'Wins':>8}{'Losses':>8}{'Delta':>8}")
        for pillar, max_val in [("indicators", 4.0), ("trend", 3.0),
                                 ("volume", 2.0), ("conviction", 1.0)]:
            wdata = pw.get(pillar, {})
            ldata = pl.get(pillar, {})
            wavg = (wdata["w_sum"] / wdata["w_weight"]) if wdata.get("w_weight", 0) > 0 else 0
            lavg = (ldata["w_sum"] / ldata["w_weight"]) if ldata.get("w_weight", 0) > 0 else 0
            delta = wavg - lavg
            sign = "+" if delta >= 0 else ""
            L.append(f"  {pillar.title():<20}(/{max_val:.0f}){wavg:>6.1f}{lavg:>8.1f}{sign}{delta:>7.1f}")
        L.append("")

    # ------------------------------------------------------------------
    #  PLAYS (by tier, then overall)
    # ------------------------------------------------------------------
    pts = rs.get("play_types", {})
    tier_plays = rs.get("tier_plays", {})
    if pts:
        L.append("=" * 64)
        L.append("  PLAYS")
        L.append("=" * 64)
        # Per-tier breakdown first (A/B only — C-tier not scored)
        for tier in ("A", "B"):
            tp = tier_plays.get(tier, {})
            if not tp:
                continue
            sorted_tp = sorted(tp.items(), key=lambda x: x[1].get("count", 0), reverse=True)
            L.append(f"  --- {tier}-TIER ---")
            for pt_key, pt_data in sorted_tp:
                wins = pt_data.get("wins", 0)
                losses = pt_data.get("losses", 0)
                if wins + losses == 0:
                    continue
                label = PLAY_TYPES.get(pt_key, pt_key)
                L.append(f"    {label:<28}{_wr(wins, losses)}")
            L.append("")
        # Overall totals
        sorted_plays = sorted(pts.items(), key=lambda x: x[1].get("count", 0), reverse=True)
        L.append(f"  --- ALL TIERS ---")
        for pt_key, pt_data in sorted_plays:
            count = pt_data.get("count", 0)
            wins = pt_data.get("wins", 0)
            losses = count - wins
            label = PLAY_TYPES.get(pt_key, pt_key)
            L.append(f"    {label:<28}{_wr(wins, losses)}")
        L.append("")

    # ------------------------------------------------------------------
    #  SIGNALS (by tier, then overall)
    # ------------------------------------------------------------------
    sigs = rs.get("signals", {})
    tier_sigs = rs.get("tier_signals", {})
    if sigs:
        L.append("=" * 64)
        L.append("  SIGNALS")
        L.append("=" * 64)
        adaptive_wts = state.get("regime_weights_" + state.get("_last_regime", "trend"), {})
        # Per-tier breakdown first (A/B only — C-tier not scored)
        for tier in ("A", "B"):
            ts = tier_sigs.get(tier, {})
            if not ts:
                continue
            sorted_ts = sorted(ts.items(), key=lambda x: x[1].get("count", 0), reverse=True)
            L.append(f"  --- {tier}-TIER ---")
            for sig_name, sig_data in sorted_ts:
                wins = sig_data.get("wins", 0)
                losses = sig_data.get("losses", 0)
                if wins + losses == 0:
                    continue
                L.append(f"    {sig_name:<22}{_wr(wins, losses)}")
            L.append("")
        # Overall totals with adaptive weights
        sorted_sigs = sorted(sigs.items(), key=lambda x: x[1].get("count", 0), reverse=True)
        L.append(f"  --- ALL TIERS ---")
        for sig_name, sig_data in sorted_sigs:
            count = sig_data.get("count", 0)
            wins = sig_data.get("wins", 0)
            losses = count - wins
            wt = adaptive_wts.get(sig_name, SIGNAL_WEIGHTS.get(sig_name, 0.5))
            L.append(f"    {sig_name:<22}{_wr(wins, losses)}  wt:{wt:.2f}")
        L.append("")

    # ------------------------------------------------------------------
    #  SCORE BUCKETS (win rate by score range)
    # ------------------------------------------------------------------
    sbs = rs.get("score_buckets", {})
    if sbs:
        sorted_sb = sorted(sbs.items(), key=lambda x: float(x[0]))
        L.append("=" * 64)
        L.append("  WIN RATE BY SCORE")
        L.append("=" * 64)
        for sb_key, sb_data in sorted_sb:
            count = sb_data.get("count", 0)
            wr = (sb_data["w_wins"] / sb_data["w_total"] * 100) if sb_data.get("w_total", 0) > 0 else 0
            L.append(f"  Score {float(sb_key):>4.1f}   {count:>3} picks   {wr:>5.0f}% weighted win rate")
        L.append("")

    # ------------------------------------------------------------------
    #  REGIME
    # ------------------------------------------------------------------
    rt = rs.get("regime_total", {})
    if rt:
        L.append("=" * 64)
        L.append("  REGIME")
        L.append("=" * 64)
        for regime, rdata in sorted(rt.items()):
            count = rdata.get("count", 0)
            wr = (rdata["w_wins"] / rdata["w_total"] * 100) if rdata.get("w_total", 0) > 0 else 0
            L.append(f"  {regime:<20}{count:>3} picks   {wr:>5.0f}% weighted win rate")
        L.append("")

    # ------------------------------------------------------------------
    #  MARKET INTELLIGENCE
    # ------------------------------------------------------------------
    _intel_factors = [
        ("SHORT INTEREST", "si_buckets"),
        ("FLOAT SIZE", "float_buckets"),
        ("ANALYST UPSIDE", "analyst_buckets"),
        ("INSTITUTIONAL OWNERSHIP", "inst_buckets"),
        ("BETA", "beta_buckets"),
    ]
    _any_intel = any(rs.get(bk) for _, bk in _intel_factors)
    if _any_intel:
        L.append("=" * 64)
        L.append("  MARKET INTELLIGENCE")
        L.append("=" * 64)
        for label, bucket_key in _intel_factors:
            buckets = rs.get(bucket_key, {})
            if not buckets:
                continue
            sorted_b = sorted(buckets.items(), key=lambda x: x[0])
            L.append(f"  --- {label} ---")
            for bk, bd in sorted_b:
                count = bd.get("count", 0)
                if count < 3:
                    continue
                wr = (bd["w_wins"] / bd["w_total"] * 100) if bd.get("w_total", 0) > 0 else 0
                L.append(f"    {bk:<20}{count:>3} picks   {wr:>5.0f}% weighted win rate")
            L.append("")

    L.append("=" * 64)
    L.append("  END OF METRICS")
    L.append("=" * 64)
    L.append("")

    _write_metrics_file(L)


def _write_metrics_file(lines: list):
    """Write metrics report to reports directory."""
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / "metrics.txt"
    path.write_text("\n".join(lines), encoding="utf-8")


def write_adaptive_variables_report():
    """Write adaptive variables report — current value, previous value,
    and date of last change for every EMA-damped variable."""
    state = _load_adaptive_state()
    history = state.get("history", {})
    last_updated = state.get("last_updated", "unknown")

    L = []
    L.append("=" * 64)
    L.append("  ADAPTIVE VARIABLES")
    L.append(f"  {_datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    L.append(f"  Last updated: {last_updated}")
    L.append("=" * 64)

    if not history:
        L.append("")
        L.append("  No adaptive variables yet — run the scanner to build data.")
        L.append("")
        _write_variables_file(L)
        return

    categories = [
        ("Signal Weights", "signal."),
        ("Pillar Multipliers", "pillar."),
        ("Win Rate Curve", "winrate"),
        ("RS Adjustments", "rs."),
        ("Regime Drift", "regime_drift"),
        ("Stops", "stop_"),
        ("Market Intelligence", "intel."),
    ]

    for cat_name, prefix in categories:
        cat_keys = sorted(k for k in history if k.startswith(prefix))
        if not cat_keys:
            continue
        L.append("")
        L.append(f"  {cat_name.upper()}")
        L.append(f"  {'-' * 60}")
        for key in cat_keys:
            vals = history[key]
            if not vals:
                continue
            current = vals[-1]
            previous = vals[-2] if len(vals) >= 2 else current
            # Name: strip prefix for cleaner display
            name = key
            for p in ("signal.", "pillar.", "winrate_", "rs.", "regime_drift_", "stop_", "intel."):
                if key.startswith(p):
                    name = key[len(p):]
                    break
            if name == "":
                name = key

            delta = current - previous
            changed = abs(delta) > 0.00005
            sign = "+" if delta >= 0 else ""
            change_str = f"{sign}{delta:.4f}" if changed else "unchanged"
            L.append(f"    {name:<24}{current:>8.4f}  (prev: {previous:.4f}  {change_str})")

    # Also show derived values not in history
    L.append("")
    L.append(f"  DERIVED VALUES")
    L.append(f"  {'-' * 60}")
    stop_m = state.get("stop_multiplier")
    if stop_m is not None:
        L.append(f"    {'stop_multiplier':<24}{stop_m:>8.4f}")
    # Per-regime stop multipliers
    rs = state.get("running_stats", {})
    regime_stops = rs.get("regime_stop", {})
    for rname in sorted(regime_stops):
        rsb = regime_stops[rname]
        if rsb.get("count", 0) >= 5:
            rw, rl = rsb.get("w_wins", 0), rsb.get("w_losses", 0)
            rmult, rconf = _stop_mult_from_data(rw, rl)
            L.append(f"    {'stop_' + rname:<24}{rmult:>8.4f}  ({rsb['count']} picks, conf: {rconf:.2f})")
    dvt = state.get("distrib_vol_threshold")
    if dvt is not None:
        L.append(f"    {'distrib_vol_threshold':<24}{dvt:>8.4f}")
    wrc = state.get("win_rate_curve", {})
    if wrc:
        for score_key in sorted(wrc, key=lambda x: float(x)):
            L.append(f"    {'wr@' + score_key:<24}{wrc[score_key]:>8.1%}")

    # Sigmoid params (Phase 3)
    sig_params = state.get("sigmoid_params")
    if sig_params:
        L.append("")
        L.append(f"  SIGMOID CALIBRATION")
        L.append(f"  {'-' * 60}")
        for pname in ("midpoint", "slope", "floor", "ceiling"):
            cur = sig_params.get(pname, DEFAULT_SIGMOID_PARAMS[pname])
            default = DEFAULT_SIGMOID_PARAMS[pname]
            delta = cur - default
            sign = "+" if delta >= 0 else ""
            change_str = f"{sign}{delta:.4f}" if abs(delta) > 0.00005 else "unchanged"
            L.append(f"    {pname:<24}{cur:>8.4f}  (default: {default:.4f}  {change_str})")

    # Factor weights (Phase 2)
    factor_wts = get_adaptive_factor_weights()
    if factor_wts:
        L.append("")
        L.append(f"  FACTOR WEIGHTS")
        L.append(f"  {'-' * 60}")
        for fname in sorted(factor_wts):
            cur = factor_wts[fname]
            base = FACTOR_WEIGHTS.get(fname, cur)
            delta = cur - base
            sign = "+" if delta >= 0 else ""
            change_str = f"{sign}{delta:.4f}" if abs(delta) > 0.00005 else "unchanged"
            L.append(f"    {fname:<24}{cur:>8.4f}  (base: {base:.4f}  {change_str})")

    L.append("")
    L.append("=" * 64)
    L.append("  END OF ADAPTIVE VARIABLES")
    L.append("=" * 64)
    L.append("")

    _write_variables_file(L)


def _write_variables_file(lines: list):
    """Write adaptive variables report to reports directory."""
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / "adaptive_variables.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
