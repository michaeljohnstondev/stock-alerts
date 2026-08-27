import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import questionary
import yfinance as yf

from .constants import (
    SIGNAL_WEIGHTS, PILLAR_DEFAULTS, REPORTS_DIR, EARNINGS_NOTE_DAYS, EARNINGS_WARN_DAYS,
    PLAY_TYPES,
    CONFLICTING_SIGNAL_MAX_PENALTY,
    OVERBOUGHT_EXTENDED_PENALTY, OVERBOUGHT_ONLY_PENALTY, EXTENDED_ONLY_PENALTY,
    TIER_B_MIN, REGIME_CHOP_CAP, MKT_DISAGREE_CAP, DIVERGENCE_MKT_CAP,
    CHOP_VOL_MIN, CHOP_TREND_VOL_MIN, CHOP_TREND_PLAY_DISCOUNT,
    RANGE_PLAY_SIGNALS, CHOP_RANGE_BOOST,
    EV_TIER_C_MAX, EV_TIER_A_MIN,
    SECTOR_CONCENTRATION_MAX,
)
from .data import (
    load_config, fetch_stock_data, get_ticker_info, get_live_price,
    _alpaca_quote, get_fundamentals, get_market_intel,
    reset_api_counter, get_api_call_count,
)
from .indicators import (
    calculate_rsi, calculate_macd, calculate_bollinger_bands,
    calculate_stochastic, calculate_adx, calculate_atr,
    calculate_200ma, calculate_volume_ratio,
    find_nearest_support, find_nearest_resistance,
    find_swing_low, find_swing_high,
)
from .market import (
    detect_market_regime, check_regime_change,
    get_market_trend, calculate_sector_rotation, check_sector_changes,
    get_market_context, calculate_relative_strength,
    check_earnings_proximity, get_higher_timeframe_trend,
    detect_rotation_divergence, market_agrees,
    fetch_earnings_calendar, reset_earnings_calendar_cache,
    OFFENSIVE_SECTORS, DEFENSIVE_SECTORS,
)
from .adaptive import (
    get_regime_signal_weights, get_adaptive_pillars,
    get_adaptive_win_rates, get_adaptive_stop_multiplier,
    get_adaptive_distrib_threshold,
    get_adaptive_health, write_metrics_report, write_adaptive_variables_report,
    track_rs_and_sector_health, get_cached_adaptive_values,
    bootstrap_running_stats, consume_scored_picks,
)
from .analysis import analyze_ticker, classify_play, reset_play_type_wr_cache
from .scoring import calculate_score, calculate_ev, get_tier, _get_setup_context, score_value, compute_intel_adjustment, kelly_fraction
from .preferences import (
    load_user_preferences, record_favorites, record_favorite_setups,
    compute_preference_boost, load_favorites, add_favorites, remove_favorites,
    load_alerts, add_alert, remove_alert,
    record_plays, evaluate_plays, get_play_stats,
)
from .signals import generate_signals
from .tracking import save_picks, score_previous_picks, log_trade_results, save_missed_movers, analyze_pattern_gaps, analyze_losers, repair_signal_indicators, backfill_pick_fields, mark_picks_favorited, mark_picks_excluded
from .report import write_report, write_missed_movers_report, write_losers_report, save_scan_snapshot


# ---------------------------------------------------------------------------
# Signal Leaderboards — per-signal top-10 for QA / validation
# ---------------------------------------------------------------------------

def build_signal_leaderboards(entries: list, top_n: int = 10) -> dict:
    """Build per-signal-type leaderboards from all ticker data.

    Returns dict keyed by (indicator, direction) -> sorted list of top entries.
    Also includes synthetic leaderboards for volume and proximity metrics.
    """
    from collections import defaultdict

    # --- Signal-based leaderboards (from fired signals) ---
    buckets = defaultdict(list)
    for entry in entries:
        for sig in entry.get("signals", []):
            key = (sig["indicator"], sig["direction"])
            _entry_metrics = entry.get("metrics") or {}
            buckets[key].append({
                "ticker": entry["ticker"],
                "price": entry["price"],
                "strength": sig.get("strength", 0),
                "detail": sig.get("detail", ""),
                "meta": sig.get("meta", {}),
                "vol_ratio": entry.get("vol_ratio"),
                "rs_raw": entry.get("rs_raw"),
                "n_signals": len(entry.get("signals", [])),
                "trend_200": entry.get("trend_200"),
                "rsi": _entry_metrics.get("rsi"),
                "support": _entry_metrics.get("support"),
                "value_grade": entry.get("value_grade"),
                "earnings": entry.get("earnings"),
                "revenue": entry.get("revenue"),
            })

    # Sort each bucket by contextual key
    sorted_boards = {}
    # Featured plays keep full pool (confluence needs all overlaps, display caps at 15)
    _featured_keys = {
        ("M on Uptrend", "bullish"),
        ("M on Downtrend", "bullish"),
        ("M Breakout Up", "bullish"),
        ("M Breakout Down", "bullish"),
        ("Support/Resist", "bullish"),
        ("Double Bottom", "bullish"),
        ("5 EMA Touch", "bullish"),
        ("200 MA Touch", "bullish"),
        ("EMA21 Pullback", "bullish"),
        ("RSI", "bullish"),
        ("Earnings Beat Drift", "bullish"),
        ("Earnings Overreaction", "bullish"),
        ("Earnings Anticipation", "bullish"),
    }
    for key, items in buckets.items():
        indicator = key[0]
        sort_fn = _get_leaderboard_sort_fn(indicator)
        items.sort(key=sort_fn)
        if key in _featured_keys:
            sorted_boards[key] = items  # full pool for confluence
        else:
            sorted_boards[key] = items[:top_n]

    # --- Synthetic leaderboards (raw metrics, all stocks) ---

    # Volume — highest volume ratio
    vol_items = [
        {"ticker": e["ticker"], "price": e["price"], "vol_ratio": e["vol_ratio"],
         "rs_raw": e.get("rs_raw"), "n_signals": len(e.get("signals", [])),
         "detail": f"Vol: {e['vol_ratio']:.1f}x normal" if e.get("vol_ratio") else ""}
        for e in entries if e.get("vol_ratio") and e["vol_ratio"] > 0
    ]
    vol_items.sort(key=lambda x: x["vol_ratio"], reverse=True)
    sorted_boards[("_volume", "")] = vol_items[:top_n]

    # Support proximity — closest to support (above it)
    sup_items = []
    for e in entries:
        sup = (e.get("metrics") or {}).get("support")
        if sup and sup > 0 and e["price"] > 0:
            dist = (e["price"] - sup) / sup * 100
            if 0 < dist < 10:  # within 10% above support
                sup_items.append({
                    "ticker": e["ticker"], "price": e["price"],
                    "level": sup, "dist_pct": round(dist, 2),
                    "rs_raw": e.get("rs_raw"), "n_signals": len(e.get("signals", [])),
                    "detail": f"{dist:.1f}% above support ${sup:.2f}",
                })
    sup_items.sort(key=lambda x: x["dist_pct"])
    sorted_boards[("_support_proximity", "")] = sup_items[:top_n]

    # Resistance proximity — closest to resistance (below it)
    res_items = []
    for e in entries:
        res = (e.get("metrics") or {}).get("resistance")
        if res and res > 0 and e["price"] > 0:
            dist = (res - e["price"]) / res * 100
            if 0 < dist < 10:
                res_items.append({
                    "ticker": e["ticker"], "price": e["price"],
                    "level": res, "dist_pct": round(dist, 2),
                    "rs_raw": e.get("rs_raw"), "n_signals": len(e.get("signals", [])),
                    "detail": f"{dist:.1f}% below resistance ${res:.2f}",
                })
    res_items.sort(key=lambda x: x["dist_pct"])
    sorted_boards[("_resistance_proximity", "")] = res_items[:top_n]

    # 200 MA proximity — closest to 200 MA (either side)
    ma200_items = []
    for e in entries:
        ma = (e.get("metrics") or {}).get("ma200_val")
        if ma and ma > 0 and e["price"] > 0:
            dist = (e["price"] - ma) / ma * 100
            if abs(dist) < 5:  # within 5%
                side = "above" if dist > 0 else "below"
                ma200_items.append({
                    "ticker": e["ticker"], "price": e["price"],
                    "level": ma, "dist_pct": round(abs(dist), 2), "dist_signed": round(dist, 2),
                    "rs_raw": e.get("rs_raw"), "n_signals": len(e.get("signals", [])),
                    "detail": f"{abs(dist):.1f}% {side} 200 MA (${ma:.2f})",
                })
    ma200_items.sort(key=lambda x: x["dist_pct"])
    sorted_boards[("_200ma_proximity", "")] = ma200_items[:top_n]

    # EMA21 proximity — closest to EMA21 (either side)
    ema21_items = []
    for e in entries:
        ema = (e.get("metrics") or {}).get("ema21_val")
        if ema and ema > 0 and e["price"] > 0:
            dist = (e["price"] - ema) / ema * 100
            if abs(dist) < 3:  # within 3%
                side = "above" if dist > 0 else "below"
                ema21_items.append({
                    "ticker": e["ticker"], "price": e["price"],
                    "level": ema, "dist_pct": round(abs(dist), 2), "dist_signed": round(dist, 2),
                    "rs_raw": e.get("rs_raw"), "n_signals": len(e.get("signals", [])),
                    "detail": f"{abs(dist):.1f}% {side} EMA21 (${ema:.2f})",
                })
    ema21_items.sort(key=lambda x: x["dist_pct"])
    sorted_boards[("_ema21_proximity", "")] = ema21_items[:top_n]

    return sorted_boards


def _get_leaderboard_sort_fn(indicator: str):
    """Return a sort key function for a given signal type.
    Returns (key_fn) — all sort descending by default via negation."""
    if indicator == "RSI":
        # Most extreme RSI first (furthest from 50)
        return lambda x: -abs(x.get("meta", {}).get("value", 50) - 50)
    elif indicator == "MACD":
        # Strongest histogram magnitude
        return lambda x: -abs(x.get("meta", {}).get("hist_now", 0))
    elif indicator == "Support/Resist":
        # Closest to level
        return lambda x: abs(x.get("meta", {}).get("distance_pct", 999))
    elif indicator in ("Accumulation", "Distribution"):
        # Most conditions met, then strength
        return lambda x: (-x.get("meta", {}).get("conditions_met", 0), -x.get("strength", 0))
    elif indicator == "Double Bottom":
        # Deepest neckline (most defined pattern)
        return lambda x: -x.get("meta", {}).get("neckline_pct", 0)
    elif indicator == "Double Top":
        return lambda x: -x.get("meta", {}).get("neckline_pct", 0)
    elif indicator == "Higher Lows":
        return lambda x: -x.get("meta", {}).get("n_higher_lows", 0)
    elif indicator == "Dist Breakdown":
        return lambda x: -x.get("meta", {}).get("n_lower_highs", 0)
    elif indicator == "Deep Pullback":
        # Deepest pullback first (most negative %)
        return lambda x: x.get("meta", {}).get("deepest_pct", 0)
    elif indicator == "Volume Climax":
        return lambda x: -x.get("meta", {}).get("vol_mult", 0) if "vol_mult" in x.get("meta", {}) else -x.get("strength", 0)
    elif indicator in ("M on Uptrend", "W on Downtrend"):
        # Closest to trendline first, then best pattern quality
        return lambda x: (x.get("meta", {}).get("proximity_atr", 99),
                          -x.get("meta", {}).get("pattern_score", 0))
    elif indicator == "M on Downtrend":
        # Closest to projected V3 first, then best pattern quality
        return lambda x: (x.get("meta", {}).get("proximity_atr", 99),
                          -x.get("meta", {}).get("pattern_score", 0))
    elif indicator in ("M Breakout Up", "M Breakout Down"):
        # Closest to breakout point first, then best volume
        return lambda x: (x.get("meta", {}).get("breakout_pct", 99),
                          -x.get("meta", {}).get("vol_ratio", 0))
    elif indicator in ("5 EMA Touch", "200 MA Touch"):
        # Closest to EMA first
        return lambda x: x.get("meta", {}).get("dist_pct", 99)
    elif indicator in ("Earnings Beat Drift", "Earnings Overreaction"):
        # Highest D first — most opportunity
        return lambda x: -x.get("meta", {}).get("drift", 0)
    elif indicator == "Earnings Anticipation":
        # Lowest RSI first — most oversold
        return lambda x: x.get("meta", {}).get("rsi", 99)
    else:
        # Default: highest strength first
        return lambda x: -x.get("strength", 0)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_check(config: dict):
    print(f"\n[{datetime.now()}] Running stock check...")
    reset_api_counter()
    reset_play_type_wr_cache()
    reset_earnings_calendar_cache()

    # Regime detection first (also provides SPY data we'll reuse)
    print("  Detecting market regime...")
    regime_info = detect_market_regime(config)
    regime = regime_info["regime"]
    spy_df = regime_info["spy_df"]
    regime_labels = {
        "chop": f"CHOP (ADX {regime_info['adx']:.1f}) — structure trades only, scores capped",
        "range": f"RANGE (ADX {regime_info['adx']:.1f}) — fade extremes",
        "trend": f"TREND (ADX {regime_info['adx']:.1f}) — continuation setups",
        "unknown": "UNKNOWN — no regime filter applied",
    }
    print(f"  Regime:  {regime_labels.get(regime, regime)}")

    # Adaptive regime thresholds (show when shifted from defaults)
    chop_th = regime_info.get("chop_threshold", 18.0)
    trend_th = regime_info.get("trend_threshold", 25.0)
    if abs(chop_th - 18.0) > 0.5 or abs(trend_th - 25.0) > 0.5:
        print(f"  Adaptive regime: chop<{chop_th:.0f}  range {chop_th:.0f}-{trend_th:.0f}  trend>{trend_th:.0f}  (default: 18/25)")

    # Regime change detection
    regime_change = check_regime_change(regime, regime_info["adx"])
    if regime_change["changed"]:
        print(f"  >>> REGIME CHANGE: {regime_change['from_regime'].upper()} -> {regime_change['to_regime'].upper()}")

    # Market alignment — reuse SPY data from regime detection (Fix 5: no duplicate fetch)
    print("  Checking market alignment...")
    market_trends = get_market_trend(config, prefetched={"SPY": spy_df})
    for sym, trend in market_trends.items():
        print(f"    {sym}: {trend}")

    # Sector rotation analysis
    print("  Calculating sector rotation...")
    sector_rotation = calculate_sector_rotation(spy_df)
    sector_changes = {"has_prev": False, "changes": {}, "transitions": []}
    if sector_rotation:
        spy_close = float(spy_df["Close"].iloc[-1]) if not spy_df.empty else 0
        sector_changes = check_sector_changes(sector_rotation, spy_close=spy_close)
        ranked = sorted(sector_rotation.items(), key=lambda x: x[1].get("rank", 99))
        for sector, data in ranked[:3]:
            sc = sector_changes.get("changes", {}).get(sector, {})
            rd = sc.get("rank_delta", 0)
            arrow = f" ^{rd}" if rd > 0 else f" v{abs(rd)}" if rd < 0 else ""
            print(f"    #{data['rank']} {sector}: {data['status'].upper()} 1d:{data.get('ret_1d', 0):+.1f}% (comp:{data['composite']:+.1f}){arrow}")
        print(f"    ... ({len(sector_rotation)} sectors ranked)")
        for t in sector_changes.get("transitions", []):
            print(f"    >>> SECTOR SHIFT: {t['sector']}: {t['from'].upper()} -> {t['to'].upper()}")

    # Rotation divergence — detect when sector rotation contradicts market trend
    rot_divergence = detect_rotation_divergence(market_trends, sector_rotation)
    if rot_divergence["divergent"]:
        print(f"    !! {rot_divergence['reason']}")

    # Market context — rates, gold, crypto
    print("  Fetching market context (bonds, gold, crypto)...")
    market_context = get_market_context()
    for sym, info in market_context.items():
        print(f"    {info['label']}: {info['trend'].upper()} ({info['pct_above_200']:+.1f}% vs 200 MA)")

    # Earnings calendar — batch fetch from Yahoo (past 7d + next 5d)
    # Primary safety net for unreliable yfinance per-ticker earnings_dates
    print("  Fetching earnings calendar...")
    earnings_cal = fetch_earnings_calendar(days_ahead=EARNINGS_WARN_DAYS, days_back=7)
    if earnings_cal:
        print(f"    {len(earnings_cal)} companies with earnings in past 7d + next {EARNINGS_WARN_DAYS}d")
    else:
        print("    Could not fetch earnings calendar (will rely on per-ticker lookup)")

    # One-time data repair: fix signal names that were stored with wrong direction
    repaired = repair_signal_indicators()
    if repaired:
        print(f"  Data repair: fixed signal_indicators on {repaired} picks")

    # Backfill missing fields on old picks (pre-regime-detection)
    backfilled = backfill_pick_fields(regime=regime, regime_adx=regime_info["adx"])
    if backfilled:
        print(f"  Data repair: backfilled {backfilled} picks with missing fields")

    # Score previous picks before scanning new ones
    print("  Scoring previous picks...")
    scorecard_data = score_previous_picks()
    if scorecard_data.get("has_data"):
        overall = scorecard_data["stats"]["overall"]
        w10 = overall.get("wins_10d", 0)
        l10 = overall.get("losses_10d", 0)
        total_picks = overall.get("total", 0)
        if (w10 + l10) > 0:
            print(f"    {total_picks} picks tracked, 10d win rate: {w10/(w10+l10)*100:.0f}% ({w10}W/{l10}L)")
        else:
            print(f"    {total_picks} picks tracked (no completed yet)")
    else:
        print("    No previous picks to score")

    # Log newly resolved trades to winners.txt / losers.txt
    n_logged = log_trade_results()
    if n_logged > 0:
        print(f"    Logged {n_logged} new trade result(s) to winners/losers.txt")

    # Losers post-mortem analysis
    losers_data = analyze_losers()
    if losers_data.get("has_data"):
        n_loss = losers_data["total_losses"]
        n_scored = losers_data["total_scored"]
        print(f"    Losers analysis: {n_loss}/{n_scored} picks lost — see losers_analysis.txt")
        write_losers_report(losers_data)

    # --- CONSUME SCORED PICKS INTO RUNNING STATS ---
    # Must happen after scoring + logging + losers analysis (they need individual picks)
    # but before adaptive functions read running_stats
    bootstrapped = bootstrap_running_stats()
    if bootstrapped:
        print("  Bootstrapped running stats from existing picks history")
    n_consumed = consume_scored_picks()
    if n_consumed > 0:
        print(f"    Consumed {n_consumed} scored picks into running stats")

    all_results = []
    all_developing = []
    all_dip_watch = []
    all_approaching = []
    all_movers = []
    tickers = config["tickers"]
    total = len(tickers)

    # --- ADAPTIVE LEARNING: load all learned parameters ---
    # When new data was consumed (or bootstrapped), recompute with EMA dampening.
    # Otherwise read cached values — prevents phantom drift between same-day scans.
    _new_data = n_consumed > 0 or bootstrapped
    if _new_data:
        # 1. Signal weights (which signals predict winners)
        adaptive_wts = get_regime_signal_weights(regime)
        # 2. Pillar multipliers (which I/T/V/C pillars predict winners)
        pillar_m = get_adaptive_pillars()
        # 3. Win rate curve (empirical win rates by score bucket)
        adaptive_wr = get_adaptive_win_rates()
        # 4. Stop distance multiplier (learned from stop/TP hit rates)
        stop_m = get_adaptive_stop_multiplier(regime)
        # 5. Distribution volume threshold (learned from distribution signal outcomes)
        distrib_thresh = get_adaptive_distrib_threshold()
    else:
        cached = get_cached_adaptive_values(regime)
        adaptive_wts = cached["weights"]
        pillar_m = cached["pillars"]
        adaptive_wr = cached["win_rates"]
        stop_m = cached["stop_mult"]
        distrib_thresh = cached["distrib_thresh"]

    adapted_count = sum(1 for k in adaptive_wts if adaptive_wts[k] != SIGNAL_WEIGHTS.get(k))
    if adapted_count > 0:
        print(f"  Adaptive weights: {adapted_count} signals adjusted (regime: {regime})")

    pillar_adjusted = sum(1 for k, v in pillar_m.items() if abs(v - 1.0) > 0.01)
    if pillar_adjusted > 0:
        print(f"  Adaptive pillars: {pillar_adjusted} pillars adjusted from performance data")
        for k, v in pillar_m.items():
            if abs(v - 1.0) > 0.01:
                print(f"    {k}: {v:.3f}x (base {PILLAR_DEFAULTS[k]:.1f} -> effective {PILLAR_DEFAULTS[k]*v:.2f})")

    empirical_buckets = sum(1 for b in adaptive_wr.values() if b["count"] >= 5)
    if empirical_buckets > 0:
        print(f"  Adaptive win rates: {empirical_buckets} score buckets have empirical data")

    if abs(stop_m - 1.5) > 0.05:
        print(f"  Adaptive stops: ATR multiplier {stop_m:.2f}x (default 1.50x)")

    if abs(distrib_thresh - 0.77) > 0.01:
        print(f"  Adaptive distrib vol: threshold {distrib_thresh:.3f} (default 0.770)")

    # 6. Track RS bucket and sector rotation health (once per scan)
    track_rs_and_sector_health()

    # 6. Adaptive learning health check
    health = get_adaptive_health()
    if health.get("has_data"):
        osc = health["oscillating_count"]
        total_tracked = health["total_tracked"]
        if osc > 0:
            print(f"  Adaptive health: {osc}/{total_tracked} variables SEESAWING")
            for k in health["oscillating"]:
                m = health["metrics"][k]
                print(f"    >> {k}: {m['reversals']} reversals in {m['runs']} runs")
        elif total_tracked > 0:
            print(f"  Adaptive health: all {total_tracked} tracked variables stable")

    # Write metrics + adaptive variables reports
    write_metrics_report()
    write_adaptive_variables_report()

    # User preference learning — boost picks matching user's favorite selections
    user_prefs = load_user_preferences()
    n_sel = user_prefs.get("profile", {}).get("total_selections", 0)
    if n_sel >= 5:
        print(f"  User preferences: {n_sel} selections loaded")

    # Parallel ticker analysis — I/O bound (yfinance), threading gives ~5-8x speedup
    max_workers = config.get("max_workers", 2)
    print(f"  Scanning {total} tickers ({max_workers} threads)...")

    def _analyze_one(ticker):
        return ticker, analyze_ticker(ticker, config, market_trends, regime, spy_df,
                                      sector_rotation=sector_rotation,
                                      adaptive_weights=adaptive_wts,
                                      pillar_mults=pillar_m,
                                      adaptive_wr=adaptive_wr,
                                      stop_mult=stop_m,
                                      user_prefs=user_prefs,
                                      rotation_divergence=rot_divergence,
                                      distrib_threshold=distrib_thresh,
                                      earnings_calendar=earnings_cal)

    hits = 0
    all_leaderboard = []  # per-ticker signal data for ALL stocks (leaderboard sections)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_analyze_one, t): t for t in tickers}
        for i, future in enumerate(as_completed(futures), 1):
            ticker = futures[future]
            try:
                _, (results, developing, move_info, dip_items, approaching, lb_entry) = future.result()
            except Exception:
                results, developing, move_info, dip_items, approaching, lb_entry = [], [], None, [], [], None
            if results:
                for r in results:
                    earn = r.get("earnings", {})
                    earn_tag = f" [EARN {earn['days_until']}d]" if earn.get("days_until") is not None and earn["days_until"] <= EARNINGS_NOTE_DAYS else ""
                    if earn.get("days_since") is not None and earn["days_since"] <= 7:
                        earn_tag += f" [EARN -{earn['days_since']}d — not tracked]"
                    if r['tier'] != 'C':
                        print(f"  [{i:3d}/{total}] {ticker:6s}  >> {r['tier']}-TIER {r['score']:.1f}/10{earn_tag}")
                        hits += 1
            elif developing:
                pass  # quiet — only print hits
            all_results.extend(results)
            all_developing.extend(developing)
            all_dip_watch.extend(dip_items)
            all_approaching.extend(approaching)
            if move_info is not None:
                all_movers.append(move_info)
            if lb_entry is not None:
                all_leaderboard.append(lb_entry)

    print(f"  Done — {hits} tickers with signals out of {total}")

    # Missed movers — big moves we didn't flag
    # Exclude earnings-driven moves (binary events the system intentionally avoids)
    picked_tickers = {r["ticker"] for r in all_results}
    min_sigs = 5 if regime == "chop" else 4
    raw_movers = [
        m for m in all_movers
        if m["ticker"] not in picked_tickers
        and not m.get("earnings_driven", False)
        and (m.get("earn_days_since") is None or m["earn_days_since"] > 14)  # no earnings within 14d
        and (m.get("earn_days_until") is None or m["earn_days_until"] > 2)  # no upcoming earnings
        and "Post-Earnings" not in m.get("signals_fired", [])  # Post-Earnings signal = earnings move
        and abs(m.get("move_1d", 0)) < 10.0  # 10%+ 1d move = news/earnings event
    ]
    # Classify each mover: catalyst / conflict / true_miss
    for m in raw_movers:
        fp = m.get("fingerprint", {})
        if fp.get("likely_catalyst"):
            m["miss_type"] = "catalyst"
        elif m.get("signal_count", 0) >= min_sigs:
            m["miss_type"] = "conflict"
        else:
            m["miss_type"] = "true_miss"
    # Exclude catalyst events from the missed movers report
    missed_movers = [m for m in raw_movers if m["miss_type"] != "catalyst"]
    missed_movers.sort(key=lambda x: abs(x["move_3d"]), reverse=True)
    catalyst_movers = [m for m in raw_movers if m["miss_type"] == "catalyst"]
    catalyst_movers.sort(key=lambda x: abs(x["move_3d"]), reverse=True)
    missed_movers = missed_movers[:10]  # top 10
    catalyst_movers = catalyst_movers[:5]  # top 5 for reference
    if missed_movers:
        n_conflict = sum(1 for m in missed_movers if m["miss_type"] == "conflict")
        n_true = sum(1 for m in missed_movers if m["miss_type"] == "true_miss")
        parts = []
        if n_true:
            parts.append(f"{n_true} undetected")
        if n_conflict:
            parts.append(f"{n_conflict} signal conflicts")
        print(f"  Missed movers: {' + '.join(parts)}")
    if catalyst_movers:
        print(f"  Catalyst events excluded: {len(catalyst_movers)} (gap + volume spike)")

    # Save missed movers to history (with fingerprints) for pattern gap learning
    save_missed_movers(missed_movers, regime)

    # Pattern gap analysis — find recurring conditions in missed movers
    pattern_gaps = analyze_pattern_gaps()
    if pattern_gaps.get("has_data"):
        n_gaps = len(pattern_gaps.get("up_gaps", [])) + len(pattern_gaps.get("dn_gaps", []))
        if n_gaps > 0:
            print(f"  Signal gaps: {n_gaps} recurring patterns in {pattern_gaps['total_movers']} missed movers")

    # Sector concentration caps — prevent over-exposure to one sector+direction
    # Sort by score descending so best picks keep their sizing
    all_results.sort(key=lambda r: r.get("score", 0), reverse=True)
    _sector_counts = {}  # (sector, direction) -> count at original sizing
    _capped = 0
    for r in all_results:
        if r.get("tier") == "C":
            continue  # C-tier not tracked, no sizing to cap
        sk = (r.get("sector", "Unknown"), r.get("direction", "bullish"))
        _sector_counts[sk] = _sector_counts.get(sk, 0) + 1
        if _sector_counts[sk] > SECTOR_CONCENTRATION_MAX:
            rt = r.get("trade_plan", {}).get("risk_tier", {})
            cur_size = rt.get("size", "")
            if "FULL" in cur_size:
                rt["size"] = "HALF 0.5%"
                rt["desc"] = rt.get("desc", "") + " (sector cap)"
                _capped += 1
            elif "HALF" in cur_size:
                rt["size"] = "QUARTER 0.25%"
                rt["desc"] = rt.get("desc", "") + " (sector cap)"
                _capped += 1
    if _capped:
        print(f"  Sector concentration: {_capped} picks downsized")

    favorites = load_favorites()

    # Build per-signal leaderboards for QA/validation view
    signal_leaderboards = build_signal_leaderboards(all_leaderboard)
    n_lb = sum(len(v) for v in signal_leaderboards.values())
    print(f"  Signal leaderboards: {len(signal_leaderboards)} signals, {n_lb} entries")

    # --- Play tracker: record new plays, evaluate old ones ---
    _price_map = {e["ticker"]: e["price"] for e in all_leaderboard}
    _earn_map = {e["ticker"]: e.get("earnings") for e in all_leaderboard}
    evaluate_plays(_price_map)
    record_plays(signal_leaderboards, earnings_map=_earn_map)
    play_stats = get_play_stats()

    # Check price alerts against scanned prices
    alerts = load_alerts()
    alert_status = []
    if alerts:
        price_map = {e["ticker"]: e["price"] for e in all_leaderboard}
        for ticker, alert_info in sorted(alerts.items()):
            cur = price_map.get(ticker)
            if cur is None:
                continue  # ticker not in scan universe
            target = alert_info["price"]
            direction = alert_info["direction"]
            if direction == "below":
                triggered = cur <= target
            else:
                triggered = cur >= target
            pct = (cur - target) / target * 100 if target > 0 else 0
            alert_status.append({
                "ticker": ticker, "price": cur, "target": target,
                "direction": direction, "triggered": triggered,
                "pct": round(pct, 1),
            })
        # Consume triggered alerts so they don't fire again
        for a in alert_status:
            if a["triggered"]:
                remove_alert(a["ticker"])

    report_path, report_text = write_report(
        all_results, market_trends, config, regime,
        regime_change=regime_change, developing=all_developing,
        sector_rotation=sector_rotation, sector_changes=sector_changes,
        market_context=market_context, rotation_divergence=rot_divergence,
        dip_watch=all_dip_watch, favorites=favorites,
        signal_leaderboards=signal_leaderboards,
        alert_status=alert_status,
        play_stats=play_stats,
        all_leaderboard=all_leaderboard,
    )

    # Write missed movers report (separate file for detailed analysis)
    mm_path = write_missed_movers_report(missed_movers, pattern_gaps, regime,
                                         catalyst_movers=catalyst_movers)

    # Sector rotation is now embedded in the main report (no separate file)

    # Save snapshot for next run's comparison
    save_scan_snapshot(all_results, regime, market_trends)

    # Save new picks for future tracking (with regime context for adaptive learning)
    save_picks(all_results, regime=regime, regime_adx=regime_info["adx"])

    try:
        print(report_text)
    except UnicodeEncodeError:
        print(report_text.encode("utf-8", errors="replace").decode("utf-8"))
    print(f"  Report saved: {report_path}")
    if all_developing:
        print(f"  VCP Watch: {len(all_developing)} stocks forming volatility contraction")
    if all_dip_watch:
        print(f"  Dip Watch: {len(all_dip_watch)} stocks extended below key MAs")

    # API call summary
    api_calls = get_api_call_count()
    print(f"  API calls this scan: {api_calls}")

    # Stash scan context for deep-dive parity
    run_check._last_spy_df = spy_df
    run_check._last_regime = regime
    run_check._last_market_trends = market_trends
    run_check._last_sector_rotation = sector_rotation
    run_check._last_adaptive_wts = adaptive_wts
    run_check._last_pillar_m = pillar_m
    run_check._last_adaptive_wr = adaptive_wr
    run_check._last_user_prefs = user_prefs
    run_check._last_rot_divergence = rot_divergence
    run_check._last_distrib_thresh = distrib_thresh

    return all_results


# ---------------------------------------------------------------------------
# Signal breakdown renderer — turns meta dicts into human-readable detail
# ---------------------------------------------------------------------------

def _render_signal_breakdown(signal: dict, df: pd.DataFrame) -> list:
    """Render signal meta into detailed human-readable breakdown lines."""
    meta = signal.get("meta")
    wt = SIGNAL_WEIGHTS.get(signal["indicator"], 0.5)
    header = f"{signal['indicator'].upper()} [{wt:.2f}]"

    if not meta:
        return [header, f"  {signal['detail']}"]

    lines = [header]

    def _bar_date(bar_offset):
        """Convert a negative bar offset (e.g. -15) to a date string."""
        try:
            idx = bar_offset if bar_offset >= 0 else len(df) + bar_offset
            if 0 <= idx < len(df):
                dt = df.index[idx]
                if hasattr(dt, 'strftime'):
                    return dt.strftime('%b %d')
            return f"bar {bar_offset}"
        except Exception:
            return f"bar {bar_offset}"

    name = signal["indicator"]

    if name == "RSI":
        lines.append(f"  Value:     {meta['value']:.1f}")
        lines.append(f"  Threshold: {meta['threshold']} ({meta['zone']})")
        lines.append(f"  Period:    {meta['period']} days")

    elif name == "MACD":
        lines.append(f"  Histogram: {meta['hist_now']:+.4f} (prev: {meta['hist_prev']:+.4f})")
        lines.append(f"  Crossed:   {'above' if meta['hist_now'] > 0 else 'below'} zero line")
        lines.append(f"  Periods:   fast={meta['fast']}, slow={meta['slow']}, signal={meta['signal']}")

    elif name == "MA Cross":
        lines.append(f"  SMA{meta['short_period']}:  ${meta['sma_short']:.2f}")
        lines.append(f"  SMA{meta['long_period']}:  ${meta['sma_long']:.2f}")
        lines.append(f"  Spread:    {meta['spread_pct']:+.2f}%")
        lines.append(f"  Held:      2 consecutive closes above/below")

    elif name == "Bollinger":
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Upper:     ${meta['upper']:.2f}")
        lines.append(f"  Lower:     ${meta['lower']:.2f}")
        lines.append(f"  Band:      {meta['band']} (period={meta['period']}, {meta['std_dev']}σ)")

    elif name == "200 MA":
        lines.append(f"  MA(200):   ${meta['ma_val']:.2f}")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Distance:  {meta['distance_pct']:+.2f}%")
        lines.append(f"  Action:    {meta['action']} (held 2 days)")

    elif name == "EMA21 Pullback":
        lines.append(f"  EMA(21):   ${meta['ema_val']:.2f}")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Pullback:  {meta['touch_bars']}d near EMA21 (deepest: ${meta['deepest_close']:.2f} vs EMA ${meta['deepest_ema']:.2f})")
        lines.append(f"  Action:    {meta['action']} off EMA21")

    elif name in ("5 EMA Touch", "200 MA Touch"):
        lines.append(f"  {meta.get('ema_label', 'EMA')}:  ${meta['ema_val']:.2f}")
        lines.append(f"  Distance:  {meta['dist_pct']:.2f}%")
        lines.append(f"  Proximity: {meta.get('proximity_score', 0):.0%}")

    elif name == "Support/Resist":
        lines.append(f"  Level:     ${meta['level']:.2f} ({meta['type']})")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Distance:  {meta['distance_pct']:.2f}%")

    elif name == "Stochastic":
        lines.append(f"  %K:        {meta['k']:.1f} (prev: {meta['k_prev']:.1f})")
        lines.append(f"  %D:        {meta['d']:.1f} (prev: {meta['d_prev']:.1f})")
        lines.append(f"  Zone:      {meta['zone']}")
        cross = "K crossed above D" if meta['k'] > meta['d'] else "K crossed below D"
        lines.append(f"  Cross:     {cross}")

    elif name == "ADX Trend":
        lines.append(f"  ADX:       {meta['adx_val']:.1f} (threshold: {meta['threshold']})")
        excess = meta['adx_val'] - meta['threshold']
        lines.append(f"  Strength:  {excess:+.1f} above threshold")

    elif name == "VCP Breakout":
        lines.append(f"  Pivot:     ${meta['pivot']:.2f}")
        lines.append(f"  Contractions: {meta['contractions']}")
        lines.append(f"  Volume:    {meta['vol_surge']:.1f}x average")
        lines.append(f"  Price:     ${meta['price']:.2f} (above pivot)")

    elif name == "Turnaround":
        lines.append(f"  52w High:  ${meta['high_52w']:.2f}")
        lines.append(f"  Drawdown:  {meta['drawdown_pct']:.0f}% (threshold: -40%)")
        lines.append(f"  Recent low:  ${meta['recent_low']:.2f} (last 20d)")
        lines.append(f"  Older low:   ${meta['older_low']:.2f} (prior 20d)")
        higher = meta['recent_low'] > meta['older_low']
        lines.append(f"  Higher low:  {'YES' if higher else 'NO'}")
        lines.append(f"  Vol surge: {meta['vol_surge']:.1f}x (threshold: 1.3x)")

    elif name == "Post-Earnings":
        lines.append(f"  Gap:       {meta['gap_pct']:+.1f}% ({meta['gap_type']})")
        lines.append(f"  Gap bar:   {_bar_date(meta['gap_bar'])}")
        lines.append(f"  Gap level: ${meta['gap_level']:.2f}")
        lines.append(f"  Price:     ${meta['price']:.2f}")

    elif name == "Breakout Retest":
        lines.append(f"  Old resistance: ${meta['old_resistance']:.2f}")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Distance:  {meta['distance_pct']:.2f}% from level")
        lines.append(f"  Retesting as support after breakout in last 10 days")

    elif name == "RSI Divergence":
        direction = signal["direction"]
        if direction == "bullish":
            lines.append(f"  Price made LOWER LOW: ${meta['recent_price']:.2f} vs ${meta['prior_price']:.2f}")
            lines.append(f"  RSI made HIGHER LOW:  {meta['recent_rsi']:.0f} vs {meta['prior_rsi']:.0f}")
        else:
            lines.append(f"  Price made HIGHER HIGH: ${meta['recent_price']:.2f} vs ${meta['prior_price']:.2f}")
            lines.append(f"  RSI made LOWER HIGH:    {meta['recent_rsi']:.0f} vs {meta['prior_rsi']:.0f}")
        lines.append(f"  RSI spread: {meta['spread']:+.1f} (threshold: 2)")
        lines.append(f"  Window:    10 bars recent vs 10 bars prior")

    elif name == "Gap Fill":
        lines.append(f"  Target:    ${meta['target']:.2f}")
        lines.append(f"  Gap size:  {meta['gap_pct']:+.1f}%")
        lines.append(f"  Gap date:  {_bar_date(meta['gap_bar'])}")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Distance:  {meta['distance_pct']:.1f}% away from fill")

    elif name == "Volume Climax":
        lines.append(f"  Volume:    {meta['vol_mult']:.1f}x average (threshold: 2.5x)")
        lines.append(f"  Avg vol:   {meta['avg_vol']:,.0f}")
        lines.append(f"  Last vol:  {meta['last_vol']:,.0f}")
        lines.append(f"  Type:      {meta['type'].replace('_', ' ')}")

    elif name == "Channel Bounce":
        lines.append(f"  Channel:   ${meta['ch_low']:.2f} — ${meta['ch_high']:.2f}")
        lines.append(f"  Width:     {meta['width_pct']:.1f}% (2-15% = valid)")
        lines.append(f"  Position:  {meta['position_pct']:.0f}% within channel")
        lines.append(f"  Price:     ${meta['price']:.2f}")

    elif name == "Golden Cross":
        label = "Golden Cross" if meta['cross_type'] == "golden" else "Death Cross"
        lines.append(f"  Type:      {label}")
        lines.append(f"  MA(50):    ${meta['ma50']:.2f}")
        lines.append(f"  MA(200):   ${meta['ma200']:.2f}")
        lines.append(f"  Spread:    {meta['spread_pct']:+.2f}%")
        lines.append(f"  Held:      2 consecutive closes")

    elif name in ("Failed Breakdown", "Failed Breakout"):
        action = "broke below then reclaimed" if name == "Failed Breakdown" else "broke above then fell back"
        lines.append(f"  Level:     ${meta['level']:.2f}")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Action:    {action} in last 5 days")
        lines.append(f"  Volume:    {meta['vol_mult']:.1f}x average (threshold: 1.3x)")

    elif name == "Double Bottom":
        lines.append(f"  First low:   ${meta['low1']:.2f} ({_bar_date(meta['bar1'])})")
        lines.append(f"  Second low:  ${meta['low2']:.2f} ({_bar_date(meta['bar2'])})")
        lines.append(f"  Spread:      {meta['spread_pct']:.2f}% (threshold: within 2%)")
        lines.append(f"  Neckline:    {meta['neckline_pct']:.1f}% rally between lows (mid high: ${meta['mid_high']:.2f})")
        lines.append(f"  Price:       ${meta['price']:.2f} (bouncing above second low)")

    elif name == "Double Top":
        lines.append(f"  First high:  ${meta['high1']:.2f} ({_bar_date(meta['bar1'])})")
        lines.append(f"  Second high: ${meta['high2']:.2f} ({_bar_date(meta['bar2'])})")
        lines.append(f"  Spread:      {meta['spread_pct']:.2f}% (threshold: within 2%)")
        lines.append(f"  Neckline:    {meta['neckline_pct']:.1f}% pullback between highs (mid low: ${meta['mid_low']:.2f})")
        lines.append(f"  Price:       ${meta['price']:.2f} (rejecting below second high)")

    elif name == "M on Uptrend":
        lines.append(f"  Peak 1:      ${meta['high1']:.2f} ({_bar_date(meta['bar1'])})")
        lines.append(f"  Peak 2:      ${meta['high2']:.2f} ({_bar_date(meta['bar2'])})")
        lines.append(f"  Valley 1:    ${meta['entry_low']:.2f} → Valley 2: ${meta['mid_low']:.2f} (ascending)")
        lines.append(f"  Spread:      {meta['spread_pct']:.1f}% (score {meta.get('spread_score', 0):.0%}, sweet spot = 6%)")
        lines.append(f"  Support:     ~${meta['projected_support']:.2f} (trendline through valleys)")
        lines.append(f"  Proximity:   {meta.get('proximity_atr', 0):.1f} ATR from support (score {meta.get('proximity_score', 0):.0%})")
        lines.append(f"  Pattern:     spread {meta.get('spread_score', 0):.0%} + dip {meta.get('dip_score', 0):.0%} = {meta.get('pattern_score', 0):.0%}")
        if meta.get('tl_touches', 0) > 0:
            lines.append(f"  Validation:  {meta['tl_touches']} trendline touch(es) (score {meta.get('validation_score', 0):.0%})")
        lines.append(f"  Price:       ${meta['price']:.2f} (pulling back to ascending support)")

    elif name == "W on Downtrend":
        lines.append(f"  Trough 1:    ${meta['low1']:.2f} ({_bar_date(meta['bar1'])})")
        lines.append(f"  Trough 2:    ${meta['low2']:.2f} ({_bar_date(meta['bar2'])})")
        lines.append(f"  Peak 1:      ${meta['entry_high']:.2f} → Peak 2: ${meta['mid_high']:.2f} (descending)")
        lines.append(f"  Spread:      {meta['spread_pct']:.1f}% (score {meta.get('spread_score', 0):.0%}, sweet spot = 6%)")
        lines.append(f"  Resistance:  ~${meta['projected_resistance']:.2f} (trendline through peaks)")
        lines.append(f"  Proximity:   {meta.get('proximity_atr', 0):.1f} ATR from resistance (score {meta.get('proximity_score', 0):.0%})")
        lines.append(f"  Pattern:     spread {meta.get('spread_score', 0):.0%} + rally {meta.get('rally_score', 0):.0%} = {meta.get('pattern_score', 0):.0%}")
        if meta.get('tl_touches', 0) > 0:
            lines.append(f"  Validation:  {meta['tl_touches']} trendline touch(es) (score {meta.get('validation_score', 0):.0%})")
        lines.append(f"  Price:       ${meta['price']:.2f} (bouncing from descending resistance)")

    elif name == "M on Downtrend":
        lines.append(f"  Valley 1:    ${meta['v1']:.2f} ({_bar_date(meta['bar_v1'])})")
        lines.append(f"  Valley 2:    ${meta['v2']:.2f} ({_bar_date(meta['bar_v2'])})")
        lines.append(f"  Projected V3: ${meta.get('projected_v3', 0):.2f}")
        lines.append(f"  Proximity:   {meta.get('proximity_atr', 0):.1f} ATR from V3 (score {meta.get('proximity_score', 0):.0%})")
        lines.append(f"  Pattern:     {meta.get('pattern_score', 0):.0%}")
        lines.append(f"  Price:       ${meta['price']:.2f}")

    elif name in ("M Breakout Up", "M Breakout Down"):
        ptype = meta.get("pattern_type", "ascending")
        lines.append(f"  Pattern:     {ptype} M (V1 ${meta['v1']:.2f}, V2 ${meta['v2']:.2f})")
        lines.append(f"  Resistance:  P2 ${meta['p2']:.2f} (broken)")
        lines.append(f"  Breakout:    +{meta['breakout_pct']:.1f}% above P2")
        lines.append(f"  Volume:      {meta['vol_ratio']:.2f}x avg (min 1.15x)")
        lines.append(f"  Price:       ${meta['price']:.2f}")

    elif name == "Trend Accel":
        lines.append(f"  MA stacking: {meta['stacking']}")
        lines.append(f"  MA(20):    ${meta['ma20']:.2f}")
        lines.append(f"  MA(50):    ${meta['ma50']:.2f}")
        lines.append(f"  MA(200):   ${meta['ma200']:.2f}")
        lines.append(f"  RSI:       {meta['rsi']:.0f} (bullish: 55-85, bearish: 15-45)")
        vol_flag = "YES" if meta.get('vol_expanding') else "no"
        lines.append(f"  Vol expansion: {meta['vol_expansion_pct']:+.1f}% (expanding: {vol_flag})")

    elif name == "Mean Reversion":
        lines.append(f"  Z-score:   {meta['z_score']:+.2f} (threshold: ±2.0)")
        lines.append(f"  MA(50):    ${meta['ma50_val']:.2f}")
        lines.append(f"  Std dev:   ${meta['std_val']:.2f}")
        lines.append(f"  Price:     ${meta['price']:.2f}")
        lines.append(f"  Distance:  {meta['distance_pct']:+.1f}% from MA(50)")

    elif name in ("Accumulation", "Early Accumulation"):
        vol_pass = "PASS" if meta.get('accum_vol') else "FAIL"
        obv_pass = "PASS" if meta.get('obv_rising') else "FAIL"
        base_pass = "PASS" if meta.get('basing') else "FAIL"
        lines.append(f"  Lookback:  {meta['lookback']} trading days")
        lines.append(f"  Buy/sell vol ratio: {meta['vol_ratio']:.2f}x (threshold: >1.3x)   {vol_pass}")
        lines.append(f"  OBV diverging up (price chg {meta['price_chg_pct']:+.1f}%):      {obv_pass}")
        lines.append(f"  Tight base: {meta['range_pct']:.1f}% range (threshold: <15%):     {base_pass}")
        lines.append(f"  Conditions: {meta['conditions_met']}/3 met (2+ needed for full signal)")

    elif name in ("Distribution", "Early Distribution"):
        vol_pass = "PASS" if meta.get('distrib_vol') else "FAIL"
        obv_pass = "PASS" if meta.get('obv_falling') else "FAIL"
        base_pass = "PASS" if meta.get('basing') else "FAIL"
        lines.append(f"  Lookback:  {meta['lookback']} trading days")
        lines.append(f"  Buy/sell vol ratio: {meta['vol_ratio']:.2f}x (threshold: <0.77x)  {vol_pass}")
        lines.append(f"  OBV diverging down (price chg {meta['price_chg_pct']:+.1f}%):     {obv_pass}")
        lines.append(f"  Tight range: {meta['range_pct']:.1f}% (threshold: <15%):           {base_pass}")
        lines.append(f"  Conditions: {meta['conditions_met']}/3 met (2+ needed for full signal)")

    elif name == "Range Compression":
        lines.append(f"  ATR(20):   ${meta['current_atr']:.2f} (6mo min: ${meta['atr_min']:.2f})")
        lines.append(f"  Compression: {meta['compression_pct']:.1f}% range")
        lines.append(f"  Highs descending: {'YES' if meta['highs_desc'] else 'NO'}")
        lines.append(f"  Lows ascending:   {'YES' if meta['lows_asc'] else 'NO'}")

    elif name == "Dist Breakdown":
        lines.append(f"  Lower highs: {meta['n_lower_highs']} consecutive (threshold: 3+)")
        lines.append(f"  Sell/buy vol ratio: {meta['dist_ratio']:.1f}x")
        lines.append(f"  Below 50 EMA: {'YES' if meta['below_ema50'] else 'NO'}")
        lines.append(f"  Near resistance: {'YES' if meta.get('near_resist') else 'NO'}")

    else:
        lines.append(f"  {signal['detail']}")

    return lines


# ---------------------------------------------------------------------------
# Deep-dive report — detailed single-stock analysis
# ---------------------------------------------------------------------------

def _deep_dive(ticker: str, spy_df: pd.DataFrame = None,
               regime: str = "unknown", market_trends: dict = None,
               sector_rotation: dict = None, adaptive_weights: dict = None,
               pillar_mults: dict = None, adaptive_wr: dict = None,
               user_prefs: dict = None, rotation_divergence: dict = None,
               distrib_threshold: float = None):
    """Generate a detailed analysis report for a single ticker.
    Returns (report_text, path, setups) where setups contains trade setup
    dicts usable for favorite marking."""
    L = []
    setups = []
    L.append("")
    L.append("  " + "=" * 60)
    L.append(f"    DEEP DIVE: {ticker}")
    L.append(f"    {datetime.now().strftime('%A, %B %d %Y  %I:%M %p')}")
    L.append("  " + "=" * 60)

    # Fetch data
    df = fetch_stock_data(ticker, period="1y")
    if df.empty or len(df) < 50:
        L.append(f"    Not enough data for {ticker}")
        return "\n".join(L), None, []

    close_price = float(df["Close"].iloc[-1])
    price = close_price

    # Live quote — Alpaca or yfinance
    alpaca_price = _alpaca_quote(ticker)
    live = get_live_price(ticker, extended_hours=True)
    live_price = live or alpaca_price

    # After hours: use close price for signals/entries (reliable),
    # show live as reference only.  During market hours: use live price.
    from zoneinfo import ZoneInfo
    _now_et = datetime.now(ZoneInfo("America/New_York"))
    _market_open = (0 <= _now_et.weekday() <= 4 and 9 <= _now_et.hour < 16
                    and not (_now_et.hour == 9 and _now_et.minute < 30))
    if _market_open and live_price:
        price = live_price
    # else: price stays as close_price

    # Sector info
    info = get_ticker_info(ticker)
    _intel = get_market_intel(ticker)  # cache read — no API call
    L.append(f"    Sector:   {info['sector']}  |  Industry: {info['industry']}")
    if _market_open:
        price_src = "Alpaca" if alpaca_price else "yfinance live"
        price_line = f"    Price:    ${price:.2f}  ({price_src})"
    else:
        price_line = f"    Price:    ${close_price:.2f}  (close)"
        if live_price and abs(live_price - close_price) / close_price > 0.005:
            _session = "Pre-market" if _now_et.hour < 9 or (_now_et.hour == 9 and _now_et.minute < 30) else "After-hours"
            price_line += f"  |  {_session}: ${live_price:.2f} ({(live_price-close_price)/close_price*100:+.1f}%)"
    L.append(price_line)

    # Fundamentals (expanded — P/E, PEG, Market Cap, margins, debt, growth)
    fund = get_fundamentals(ticker)
    fund_parts = []
    if fund.get("trailing_pe") is not None:
        fund_parts.append(f"P/E: {fund['trailing_pe']:.1f}")
    if fund.get("forward_pe") is not None:
        fund_parts.append(f"Fwd P/E: {fund['forward_pe']:.1f}")
    mkt_cap = fund.get("market_cap")
    if mkt_cap is not None:
        if mkt_cap >= 1e12:
            cap_str = f"${mkt_cap/1e12:.1f}T"
        elif mkt_cap >= 1e9:
            cap_str = f"${mkt_cap/1e9:.1f}B"
        else:
            cap_str = f"${mkt_cap/1e6:.0f}M"
        fund_parts.append(f"Cap: {cap_str}")
    if fund_parts:
        L.append(f"    {' | '.join(fund_parts)}")
    L.append("")

    # === COMPUTE CORE DATA FOR TRADE SETUPS ===
    atr = calculate_atr(df)
    atr_val = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else price * 0.02
    vol_ratio = calculate_volume_ratio(df)
    support = find_nearest_support(df, price)
    resistance = find_nearest_resistance(df, price)

    # Signals — use close price first (matches scan behavior), then re-run
    # with live price if it diverges significantly (>2%).
    _sig_config = {
        "indicators": {
            "rsi": {"enabled": True, "period": 14, "overbought": 70, "oversold": 30},
            "macd": {"enabled": True, "fast_period": 12, "slow_period": 26, "signal_period": 9},
            "moving_averages": {"enabled": True, "short_period": 20, "long_period": 50},
            "bollinger_bands": {"enabled": True, "period": 20, "std_dev": 2},
            "stochastic": {"enabled": True, "k_period": 14, "d_period": 3},
            "adx": {"enabled": True, "period": 14, "threshold": 25},
        },
        "volume": {"min_ratio": 1.0, "avg_period": 20},
    }
    signals, trend_200, vcp, recent_earnings, earn_dates, _, _dd_metrics = generate_signals(
        df, _sig_config, ticker=ticker, distrib_threshold=distrib_threshold or 0.77)

    # If live price diverged >2% from close, re-run with live price and note it
    _live_diverged = abs(price - close_price) / close_price > 0.02
    if _live_diverged:
        _close_bull = [s for s in signals if s["direction"] == "bullish"]
        _close_bear = [s for s in signals if s["direction"] == "bearish"]
        signals, trend_200, vcp, recent_earnings, earn_dates, _, _dd_metrics = generate_signals(
            df, _sig_config, ticker=ticker, live_price=price,
            distrib_threshold=distrib_threshold or 0.77)

    bull_signals = [s for s in signals if s["direction"] == "bullish"]
    bear_signals = [s for s in signals if s["direction"] == "bearish"]

    # Relative strength
    rs_raw = None
    if spy_df is not None and len(spy_df) >= 50:
        rs_raw = calculate_relative_strength(df, spy_df)

    # Higher timeframe
    htf_trend = get_higher_timeframe_trend(ticker, daily_df=df)

    # Earnings (reuse dates from generate_signals — no extra API call)
    earnings = check_earnings_proximity(ticker, prefetched_dates=earn_dates,
                                        earnings_calendar=fetch_earnings_calendar())

    # ===============================================================
    #   PRE-COMPUTE — everything needed for trade setups + report
    # ===============================================================
    _fund_bull = score_value(fund, "bullish") if fund else {"grade": "N/A"}
    _fund_bear = score_value(fund, "bearish") if fund else {"grade": "N/A"}

    # Price action context
    chg_1d = (df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100 if len(df) >= 2 else 0
    chg_5d = (df["Close"].iloc[-1] - df["Close"].iloc[-6]) / df["Close"].iloc[-6] * 100 if len(df) >= 6 else 0
    chg_20d = (df["Close"].iloc[-1] - df["Close"].iloc[-21]) / df["Close"].iloc[-21] * 100 if len(df) >= 21 else 0
    chg_60d = (df["Close"].iloc[-1] - df["Close"].iloc[-61]) / df["Close"].iloc[-61] * 100 if len(df) >= 61 else 0
    high_52w = float(df["High"].max())
    low_52w = float(df["Low"].min())
    off_high = (price - high_52w) / high_52w * 100
    off_low = (price - low_52w) / low_52w * 100

    # Indicators (computed once, used by both scoring and display)
    rsi = calculate_rsi(df)
    rsi_val = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50
    rsi_zone = "OVERSOLD" if rsi_val < 30 else "OVERBOUGHT" if rsi_val > 70 else "NEUTRAL"

    macd_line, signal_line, histogram = calculate_macd(df)
    macd_val = float(macd_line.iloc[-1])
    signal_val_macd = float(signal_line.iloc[-1])
    hist_val = float(histogram.iloc[-1])
    hist_prev = float(histogram.iloc[-2]) if len(histogram) >= 2 else 0
    macd_cross = ("BULLISH CROSS" if macd_val > signal_val_macd and float(macd_line.iloc[-2]) <= float(signal_line.iloc[-2]) else
                  "BEARISH CROSS" if macd_val < signal_val_macd and float(macd_line.iloc[-2]) >= float(signal_line.iloc[-2]) else
                  "above signal" if macd_val > signal_val_macd else "below signal")
    hist_dir = "expanding" if abs(hist_val) > abs(hist_prev) else "contracting"

    stoch_k, stoch_d = calculate_stochastic(df)
    stoch_k_val = float(stoch_k.iloc[-1]) if not pd.isna(stoch_k.iloc[-1]) else 50
    stoch_d_val = float(stoch_d.iloc[-1]) if not pd.isna(stoch_d.iloc[-1]) else 50
    stoch_zone = "OVERSOLD" if stoch_k_val < 20 else "OVERBOUGHT" if stoch_k_val > 80 else "NEUTRAL"

    adx = calculate_adx(df)
    adx_val = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0
    adx_label = "STRONG TREND" if adx_val > 25 else "WEAK/RANGE" if adx_val < 18 else "MODERATE"

    avg_vol = float(df["Volume"].iloc[-20:].mean()) if len(df) >= 20 else float(df["Volume"].mean())
    atr_pct = atr_val / price * 100

    # Helper: convert bar offset to readable date
    def _bar_date(bar_offset):
        try:
            idx = bar_offset if bar_offset >= 0 else len(df) + bar_offset
            if 0 <= idx < len(df):
                dt = df.index[idx]
                if hasattr(dt, 'strftime'):
                    return dt.strftime('%b %d')
            return f"bar {bar_offset}"
        except Exception:
            return f"bar {bar_offset}"

    # Helper: thorough signal explanation from meta data
    def _explain_signal(s):
        """Turn a signal's meta dict into a thorough narrative explanation."""
        name = s["indicator"]
        meta = s.get("meta", {})
        detail = s.get("detail", "")
        direction = s["direction"]

        if not meta:
            return detail

        if name == "RSI":
            return (f"RSI({meta['period']}) is at {meta['value']:.1f}, "
                    f"{'below' if meta['value'] < 50 else 'above'} the {meta['threshold']} "
                    f"{meta['zone']} threshold. "
                    f"{'Price is washed out and due for a bounce.' if meta['value'] < 30 else 'Momentum is extended and vulnerable to a pullback.' if meta['value'] > 70 else 'Momentum is neutral.'}")

        if name == "MACD":
            cross = "crossed above" if meta['hist_now'] > 0 else "crossed below"
            return (f"MACD histogram {cross} zero at {meta['hist_now']:+.4f} "
                    f"(was {meta['hist_prev']:+.4f}). "
                    f"This is a fresh momentum shift — MACD line is now "
                    f"{'above' if meta['hist_now'] > 0 else 'below'} the signal line.")

        if name == "Support/Resist":
            level_type = meta.get("type", "level")
            return (f"Price at ${meta['price']:.2f} is {meta['distance_pct']:.1f}% from "
                    f"{level_type} at ${meta['level']:.2f}. "
                    f"{'Bouncing off this support level.' if 'support' in level_type else 'Testing this resistance level.'}")

        if name == "EMA21 Pullback":
            return (f"Price pulled back to the 21 EMA (${meta['ema_val']:.2f}) over "
                    f"{meta['touch_bars']} days, dipping as low as ${meta['deepest_close']:.2f} "
                    f"vs EMA at ${meta['deepest_ema']:.2f}. "
                    f"Now {meta['action']} off EMA21 at ${meta['price']:.2f}.")

        if name in ("5 EMA Touch", "200 MA Touch"):
            return (f"Price near the {meta.get('ema_label', 'EMA')} at "
                    f"${meta['ema_val']:.2f}, {meta['dist_pct']:.2f}% away.")

        if name == "Bollinger":
            band = "upper band" if meta['band'] == "upper" else "lower band"
            return (f"Price at ${meta['price']:.2f} is touching the {band} "
                    f"(upper ${meta['upper']:.2f}, lower ${meta['lower']:.2f}). "
                    f"{'Extended above the band — potential mean reversion.' if meta['band'] == 'upper' else 'Compressed to the lower band — potential bounce.'}")

        if name == "Stochastic":
            cross = "K crossed above D" if meta['k'] > meta['d'] else "K crossed below D"
            return (f"Stochastic %K={meta['k']:.0f} %D={meta['d']:.0f} in the {meta['zone']} zone. "
                    f"{cross} (was %K={meta['k_prev']:.0f}, %D={meta['d_prev']:.0f}). "
                    f"{'Momentum turning up from oversold.' if meta['zone'] == 'oversold' else 'Momentum turning down from overbought.' if meta['zone'] == 'overbought' else 'Momentum shift detected.'}")

        if name == "ADX Trend":
            excess = meta['adx_val'] - meta['threshold']
            return (f"ADX at {meta['adx_val']:.1f} is {excess:+.1f} above the {meta['threshold']} threshold. "
                    f"Strong directional trend in place — favor continuation setups.")

        if name == "Double Bottom":
            return (f"Two lows formed at ${meta['low1']:.2f} ({_bar_date(meta['bar1'])}) and "
                    f"${meta['low2']:.2f} ({_bar_date(meta['bar2'])}), within {meta['spread_pct']:.1f}% "
                    f"of each other. The neckline rallied {meta['neckline_pct']:.0f}% between lows "
                    f"(mid high ${meta['mid_high']:.2f}). Price now at ${meta['price']:.2f} bouncing "
                    f"above the second low.")

        if name == "Double Top":
            return (f"Two highs formed at ${meta['high1']:.2f} ({_bar_date(meta['bar1'])}) and "
                    f"${meta['high2']:.2f} ({_bar_date(meta['bar2'])}), within {meta['spread_pct']:.1f}% "
                    f"of each other. The pullback dropped {meta['neckline_pct']:.0f}% between highs "
                    f"(mid low ${meta['mid_low']:.2f}). Price now at ${meta['price']:.2f} rejecting "
                    f"below the second high.")

        if name == "M on Uptrend":
            return (f"Ascending M pattern: peaks at ${meta['high1']:.2f} ({_bar_date(meta['bar1'])}) and "
                    f"${meta['high2']:.2f} ({_bar_date(meta['bar2'])}), rising {meta['spread_pct']:.1f}%. "
                    f"Valleys at ${meta['entry_low']:.2f} and ${meta['mid_low']:.2f} define ascending support. "
                    f"Price at ${meta['price']:.2f} is near projected support ~${meta['projected_support']:.2f} — "
                    f"buying opportunity at trendline.")

        if name == "W on Downtrend":
            return (f"Descending W pattern: troughs at ${meta['low1']:.2f} ({_bar_date(meta['bar1'])}) and "
                    f"${meta['low2']:.2f} ({_bar_date(meta['bar2'])}), falling {meta['spread_pct']:.1f}%. "
                    f"Peaks at ${meta['entry_high']:.2f} and ${meta['mid_high']:.2f} define descending resistance. "
                    f"Price at ${meta['price']:.2f} is near projected resistance ~${meta['projected_resistance']:.2f} — "
                    f"selling opportunity at trendline.")

        if name in ("M Breakout Up", "M Breakout Down"):
            ptype = meta.get("pattern_type", "ascending")
            return (f"{'Ascending' if ptype == 'ascending' else 'Descending'} M pattern with valleys at "
                    f"${meta['v1']:.2f} and ${meta['v2']:.2f}. Price broke above P2 resistance "
                    f"${meta['p2']:.2f} by {meta['breakout_pct']:.1f}% on {meta['vol_ratio']:.1f}x volume.")

        if name == "Trendline Channel":
            _ch_dir = meta.get('channel_dir', 'flat')
            _n_v = meta.get('n_valleys', 0)
            _n_p = meta.get('n_peaks', 0)
            _sup = meta.get('projected_support', 0)
            _res = meta.get('projected_resistance', 0)
            _w_atr = meta.get('channel_width_atr', 0)
            if direction == "bullish":
                return (f"{_ch_dir.title()} channel with {_n_v} valley touches on support "
                        f"and {_n_p} peak touches on resistance. "
                        f"Support trendline at ~${_sup:.2f}, resistance at ~${_res:.2f} "
                        f"(channel width {_w_atr:.1f}x ATR). "
                        f"Price at ${meta['price']:.2f} is near the support boundary — "
                        f"bounce toward resistance ~${_res:.2f}.")
            else:
                return (f"{_ch_dir.title()} channel with {_n_p} peak touches on resistance "
                        f"and {_n_v} valley touches on support. "
                        f"Resistance trendline at ~${_res:.2f}, support at ~${_sup:.2f} "
                        f"(channel width {_w_atr:.1f}x ATR). "
                        f"Price at ${meta['price']:.2f} is near the resistance boundary — "
                        f"rejection toward support ~${_sup:.2f}.")

        if name == "Post-Earnings":
            gap_dir = "gapped up" if meta['gap_type'] == "up" else "gapped down"
            return (f"Stock {gap_dir} {abs(meta['gap_pct']):.1f}% on earnings ({_bar_date(meta['gap_bar'])}). "
                    f"Gap level at ${meta['gap_level']:.2f}. Price now at ${meta['price']:.2f} — "
                    f"{'pulling back toward the gap fill level.' if meta['gap_type'] == 'up' else 'bouncing into the gap fill resistance.'}")

        if name == "VCP Breakout":
            return (f"Volatility Contraction Pattern with {meta['contractions']} contractions. "
                    f"Pivot at ${meta['pivot']:.2f}, price broke out at ${meta['price']:.2f} "
                    f"on {meta['vol_surge']:.1f}x volume surge.")

        if name == "Turnaround":
            return (f"Down {meta['drawdown_pct']:.0f}% from 52w high of ${meta['high_52w']:.2f}. "
                    f"Recent low ${meta['recent_low']:.2f} is higher than prior low ${meta['older_low']:.2f} — "
                    f"higher low forming. Volume surge at {meta['vol_surge']:.1f}x average.")

        if name == "Breakout Retest":
            return (f"Broke above old resistance at ${meta['old_resistance']:.2f} in the last 10 days. "
                    f"Now retesting that level as support — price at ${meta['price']:.2f} is "
                    f"{meta['distance_pct']:.1f}% away.")

        if name == "RSI Divergence":
            if direction == "bullish":
                return (f"Price made a LOWER LOW (${meta['recent_price']:.2f} vs ${meta['prior_price']:.2f}) "
                        f"but RSI made a HIGHER LOW ({meta['recent_rsi']:.0f} vs {meta['prior_rsi']:.0f}). "
                        f"Bullish divergence — selling pressure weakening despite lower prices.")
            else:
                return (f"Price made a HIGHER HIGH (${meta['recent_price']:.2f} vs ${meta['prior_price']:.2f}) "
                        f"but RSI made a LOWER HIGH ({meta['recent_rsi']:.0f} vs {meta['prior_rsi']:.0f}). "
                        f"Bearish divergence — buying momentum fading despite higher prices.")

        if name == "Mean Reversion":
            return (f"Price at ${meta['price']:.2f} is {meta['distance_pct']:+.1f}% from MA(50) "
                    f"(${meta['ma50_val']:.2f}), z-score {meta['z_score']:+.2f}. "
                    f"{'Extremely stretched above the mean — due for a pullback.' if meta['z_score'] > 0 else 'Extremely stretched below the mean — due for a bounce.'}")

        if name == "Gap Fill":
            return (f"Open gap at ${meta['target']:.2f} ({meta['gap_pct']:+.1f}% gap from {_bar_date(meta['gap_bar'])}). "
                    f"Price at ${meta['price']:.2f} is {meta['distance_pct']:.1f}% away — gaps tend to fill.")

        if name == "Volume Climax":
            return (f"Volume spiked to {meta['vol_mult']:.1f}x average ({meta['last_vol']:,.0f} vs "
                    f"{meta['avg_vol']:,.0f} avg). {meta['type'].replace('_', ' ').title()} — "
                    f"extreme volume often marks a turning point.")

        if name == "Channel Bounce":
            return (f"Trading in a {meta['width_pct']:.1f}% channel between ${meta['ch_low']:.2f} and "
                    f"${meta['ch_high']:.2f}. Price at ${meta['price']:.2f} is at the "
                    f"{meta['position_pct']:.0f}% level within the channel. "
                    f"{'Near the bottom — bounce setup.' if meta['position_pct'] < 30 else 'Near the top — fade setup.' if meta['position_pct'] > 70 else ''}")

        if name == "MA Cross":
            return (f"SMA({meta['short_period']}) at ${meta['sma_short']:.2f} crossed "
                    f"{'above' if meta['spread_pct'] > 0 else 'below'} SMA({meta['long_period']}) "
                    f"at ${meta['sma_long']:.2f} (spread {meta['spread_pct']:+.2f}%). "
                    f"Confirmed with 2 consecutive closes.")

        if name == "200 MA":
            return (f"Price at ${meta['price']:.2f} is {meta['distance_pct']:+.2f}% from the 200 MA "
                    f"(${meta['ma_val']:.2f}). {meta['action'].title()} with 2 days held.")

        if name == "Golden Cross":
            label = "Golden Cross" if meta['cross_type'] == "golden" else "Death Cross"
            return (f"{label}: MA(50) at ${meta['ma50']:.2f} crossed "
                    f"{'above' if meta['cross_type'] == 'golden' else 'below'} MA(200) at "
                    f"${meta['ma200']:.2f} (spread {meta['spread_pct']:+.2f}%). Major trend shift.")

        if name in ("Failed Breakdown", "Failed Breakout"):
            action = ("broke below then reclaimed" if name == "Failed Breakdown"
                      else "broke above then fell back below")
            return (f"Price {action} ${meta['level']:.2f} in the last 5 days on "
                    f"{meta['vol_mult']:.1f}x volume. Now at ${meta['price']:.2f}. "
                    f"Failed moves are powerful — trapped traders must cover.")

        if name == "Trend Accel":
            return (f"MAs are stacked {meta['stacking']}: MA(20) ${meta['ma20']:.2f}, "
                    f"MA(50) ${meta['ma50']:.2f}, MA(200) ${meta['ma200']:.2f}. "
                    f"RSI at {meta['rsi']:.0f}. Volume {'expanding' if meta.get('vol_expanding') else 'stable'} "
                    f"({meta['vol_expansion_pct']:+.1f}%). Trend is accelerating.")

        if name in ("Accumulation", "Early Accumulation"):
            parts = []
            if meta.get('accum_vol'):
                parts.append(f"buy/sell volume ratio {meta['vol_ratio']:.2f}x (buyers in control)")
            if meta.get('obv_rising'):
                parts.append(f"OBV diverging up despite only {meta['price_chg_pct']:+.1f}% price change")
            if meta.get('basing'):
                parts.append(f"tight {meta['range_pct']:.1f}% base (institutions building positions quietly)")
            detail_str = ", ".join(parts) if parts else f"{meta['conditions_met']}/3 conditions met"
            return (f"{'Early signs of' if 'Early' in name else 'Confirmed'} institutional accumulation "
                    f"over {meta['lookback']} days: {detail_str}.")

        if name in ("Distribution", "Early Distribution"):
            parts = []
            if meta.get('distrib_vol'):
                parts.append(f"sell volume dominating at {meta['vol_ratio']:.2f}x ratio")
            if meta.get('obv_falling'):
                parts.append(f"OBV diverging down despite {meta['price_chg_pct']:+.1f}% price change")
            if meta.get('basing'):
                parts.append(f"tight {meta['range_pct']:.1f}% range (institutions offloading quietly)")
            detail_str = ", ".join(parts) if parts else f"{meta['conditions_met']}/3 conditions met"
            return (f"{'Early signs of' if 'Early' in name else 'Confirmed'} institutional distribution "
                    f"over {meta['lookback']} days: {detail_str}.")

        if name == "Range Compression":
            return (f"ATR squeezed to ${meta['current_atr']:.2f} (6mo min ${meta['atr_min']:.2f}). "
                    f"Range compressed to {meta['compression_pct']:.1f}%. "
                    f"Highs {'descending' if meta['highs_desc'] else 'flat'}, "
                    f"lows {'ascending' if meta['lows_asc'] else 'flat'}. "
                    f"Volatility squeeze — expect an explosive breakout.")

        if name == "Dist Breakdown":
            return (f"{meta['n_lower_highs']} consecutive lower highs with sell/buy ratio at "
                    f"{meta['dist_ratio']:.1f}x. {'Below' if meta['below_ema50'] else 'Above'} 50 EMA. "
                    f"Distribution breakdown pattern — sellers in control.")

        if name == "Higher Lows":
            return (f"{meta['n_higher_lows']} consecutive higher lows with buy/sell ratio at "
                    f"{meta['accum_ratio']:.1f}x. {'Above' if meta['above_ema50'] else 'Below'} 50 EMA. "
                    f"Structural accumulation — buyers stepping up at higher prices each time.")

        return detail

    # ===============================================================
    #   TRADE SETUPS — the main event
    # ===============================================================
    # If live price killed confluence but close-price had it, fall back to close-price signals
    _used_close_fallback = False
    if _live_diverged:
        _live_has_setup = any(len(sl) >= 3 for sl in [bull_signals, bear_signals])
        _close_has_setup = any(len(sl) >= 3 for sl in [_close_bull, _close_bear])
        if not _live_has_setup and _close_has_setup:
            bull_signals = _close_bull
            bear_signals = _close_bear
            _used_close_fallback = True

    has_setup = False
    for direction, sig_list in [("bullish", bull_signals), ("bearish", bear_signals)]:
        if len(sig_list) < 3:
            continue
        has_setup = True
        tag = "LONG" if direction == "bullish" else "SHORT"

        # Entry / stop / targets
        swing_low = find_swing_low(df, 15)
        swing_high = find_swing_high(df, 15)
        if direction == "bullish":
            stop = max(price - atr_val * 1.5, swing_low - atr_val * 0.1, support - atr_val * 0.1)
            stop = min(stop, price * 0.995)
            risk = price - stop
            if risk <= 0:
                risk = atr_val
            tp1, tp2, tp3 = price + risk, price + risk * 2, price + risk * 3
        else:
            stop = min(price + atr_val * 1.5, swing_high + atr_val * 0.1, resistance + atr_val * 0.1)
            stop = max(stop, price * 1.005)
            risk = stop - price
            if risk <= 0:
                risk = atr_val
            tp1, tp2, tp3 = price - risk, price - risk * 2, price - risk * 3

        # Score & EV
        trend_agrees = (
            (direction == "bullish" and trend_200 == "bullish") or
            (direction == "bearish" and trend_200 == "bearish")
        )
        htf_aligned = (htf_trend == "neutral" or htf_trend == direction)
        setup_context = _get_setup_context(sig_list)
        score, score_breakdown = calculate_score(sig_list, direction, trend_200, htf_trend,
                                market_trends or {}, vol_ratio, df,
                                adaptive_weights,
                                pillar_mults=pillar_mults,
                                rs_raw=rs_raw,
                                regime=regime)

        # Conflicting signal penalty
        _opp_list = bear_signals if direction == "bullish" else bull_signals
        _opp_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in _opp_list)
        _opp_names = [s["indicator"] for s in _opp_list]
        _dir_wt = sum(SIGNAL_WEIGHTS.get(s["indicator"], 0.5) for s in sig_list)
        _conflict_penalty = 0.0
        if _opp_wt > 0 and _dir_wt > 0:
            _conflict_ratio = min(_opp_wt / _dir_wt, 1.0)
            _conflict_penalty = _conflict_ratio * CONFLICTING_SIGNAL_MAX_PENALTY
            score -= _conflict_penalty
            score = max(score, 0.0)

        # Market intelligence score adjustment
        _intel_adj = compute_intel_adjustment(_intel, direction, price)
        _intel_total = _intel_adj.get("total", 0.0)
        if _intel_total != 0:
            score += _intel_total
            score = max(score, 0.0)

        # Overbought / extended penalty (with-trend only)
        if setup_context == "with_trend":
            _rsi_v = rsi_val
            _m50 = df["Close"].rolling(50).mean()
            _s50 = df["Close"].rolling(50).std()
            _zs = 0.0
            if (len(df) >= 55 and not pd.isna(_m50.iloc[-1])
                    and not pd.isna(_s50.iloc[-1]) and _s50.iloc[-1] > 0):
                _zs = (float(price) - float(_m50.iloc[-1])) / float(_s50.iloc[-1])
            if direction == "bullish":
                _is_ob = _rsi_v >= 70
                _is_ext = _zs >= 2.0
            else:
                _is_ob = _rsi_v <= 30
                _is_ext = _zs <= -2.0
            if _is_ob and _is_ext:
                score -= OVERBOUGHT_EXTENDED_PENALTY
            elif _is_ob:
                score -= OVERBOUGHT_ONLY_PENALTY
            elif _is_ext:
                score -= EXTENDED_ONLY_PENALTY
            score = max(score, 0.0)

        # 52-week high + overbought flag
        _near_52w_ob = False
        if direction == "bullish" and len(df) >= 252:
            _h52 = float(df["High"].rolling(min(252, len(df))).max().iloc[-1])
            if _h52 > 0 and (_h52 - float(price)) / _h52 <= 0.02:
                _ob_sigs = {"RSI", "Mean Reversion", "Bollinger", "Stochastic"}
                _has_ob = any(n in _ob_sigs for n in _opp_names)
                if not _has_ob:
                    _has_ob = rsi_val >= 70
                _near_52w_ob = _has_ob

        # Sector status
        _sector_status = None
        if sector_rotation and info["sector"] in sector_rotation:
            _sector_status = sector_rotation[info["sector"]].get("status")

        # Sector hard gate (don't fight sector momentum)
        _sector_skip = False
        if _sector_status == "lagging" and direction == "bullish":
            sec_data = sector_rotation.get(info["sector"], {}) if sector_rotation else {}
            stabilizing = sec_data.get("ret_1d", -99) > 0 or sec_data.get("ret_10d", -99) > 0
            if not (stabilizing and setup_context == "counter_trend"):
                _sector_skip = True
        if _sector_status == "leading" and direction == "bearish":
            sec_data = sector_rotation.get(info["sector"], {}) if sector_rotation else {}
            topping = sec_data.get("ret_1d", 99) < 0
            if not (topping and setup_context == "counter_trend"):
                _sector_skip = True

        # Regime chop filter
        if regime == "chop" and not _sector_skip:
            indicator_names = [s["indicator"] for s in sig_list]
            structure_signals = {"Support/Resist", "EMA21 Pullback", "200 MA", "MA Cross",
                                 "VCP Breakout", "Breakout Retest", "Channel Bounce",
                                 "Golden Cross", "Failed Breakdown", "Failed Breakout",
                                 "Double Bottom", "Double Top", "Higher Lows", "Dist Breakdown",
                                 "M on Uptrend", "W on Downtrend",
                                 "M Breakout Up", "M Breakout Down"}
            has_structure = any(n in structure_signals for n in indicator_names)
            is_trend_play = setup_context == "with_trend"
            vol_min = CHOP_TREND_VOL_MIN if is_trend_play else CHOP_VOL_MIN
            if vol_ratio >= 0.7 and has_structure:
                if vol_ratio < vol_min:
                    vol_gap = (vol_min - vol_ratio) / (vol_min - 0.7)
                    score -= 1.5 * vol_gap
                if is_trend_play:
                    score *= CHOP_TREND_PLAY_DISCOUNT
                range_count = sum(1 for n in indicator_names if n in RANGE_PLAY_SIGNALS)
                if range_count >= 2:
                    score += CHOP_RANGE_BOOST
                if score > TIER_B_MIN:
                    excess = score - TIER_B_MIN
                    max_excess = 10.0 - TIER_B_MIN
                    compressed = (REGIME_CHOP_CAP - TIER_B_MIN) * (excess / max_excess)
                    score = TIER_B_MIN + compressed
                score = min(score, REGIME_CHOP_CAP)

        # Market disagreement cap
        mkt_aligned = market_agrees(market_trends or {}, direction)
        _divergence_override = False
        if not mkt_aligned and rotation_divergence and rotation_divergence.get("divergent"):
            mkt_dirs = [t for t in (market_trends or {}).values() if t != "neutral"]
            mkt_dir = mkt_dirs[0] if mkt_dirs else "neutral"
            if (mkt_dir == "bullish" and direction == "bearish") or \
               (mkt_dir == "bearish" and direction == "bullish"):
                _divergence_override = True
                score = min(score, DIVERGENCE_MKT_CAP)
            else:
                score = min(score, MKT_DISAGREE_CAP)
        elif not mkt_aligned:
            score = min(score, MKT_DISAGREE_CAP)

        # Rotation divergence penalty
        if rotation_divergence and rotation_divergence.get("divergent"):
            mkt_dirs = [t for t in (market_trends or {}).values() if t != "neutral"]
            mkt_dir = mkt_dirs[0] if mkt_dirs else "neutral"
            sector_name = info["sector"]
            if (mkt_dir == "bullish" and direction == "bullish"
                    and sector_name in OFFENSIVE_SECTORS):
                score -= 0.5
            elif (mkt_dir == "bearish" and direction == "bearish"
                  and sector_name in DEFENSIVE_SECTORS):
                score -= 0.5

        tier = get_tier(score) or "—"

        # Play type + preference boost
        play_type, play_tags = classify_play(sig_list, direction, _sector_status, rs_raw)
        pref_boost = 0.0
        if user_prefs:
            pref_dict = {
                "play_type": play_type,
                "play_tags": play_tags,
                "signals": sig_list,
                "setup_context": setup_context,
                "direction": direction,
                "sector_status": _sector_status,
            }
            pref_boost = compute_preference_boost(pref_dict, user_prefs)

        ev = calculate_ev(score, risk, tp1, tp2, price, direction,
                          vol_ratio=vol_ratio, trend_agrees=trend_agrees,
                          mkt_aligned=mkt_aligned, htf_agrees=htf_aligned,
                          rs_raw=rs_raw, sector_status=_sector_status,
                          adaptive_wr=adaptive_wr, regime=regime,
                          setup_context=setup_context,
                          preference_boost=pref_boost,
                          has_opposing_signals=bool(len(_opp_list) > 0),
                          near_52w_high_overbought=_near_52w_ob,
                          divergence_override=_divergence_override,
                          intel=_intel)

        # EV tier gate
        if ev["ev_ratio"] < EV_TIER_C_MAX:
            tier = "C"
        elif ev["ev_ratio"] < EV_TIER_A_MIN and tier == "A":
            tier = "B"

        # Market headwind tier demotion
        _headwind = not mkt_aligned and not _divergence_override
        if _headwind:
            if tier == "A":
                tier = "B"
            elif tier == "B":
                tier = "C"

        rr = abs(tp2 - price) / risk if risk > 0 else 0
        play_name = PLAY_TYPES.get(play_type, play_type)

        # Direction-specific value grade
        _dir_val = _fund_bull if direction == "bullish" else _fund_bear
        _val_str = _dir_val.get("grade", "") if _dir_val.get("grade") and _dir_val["grade"] != "N/A" else ""

        # Riskiness
        risk_factors = 0
        if not trend_agrees:
            risk_factors += 1
        if vol_ratio < 1.0:
            risk_factors += 1
        if setup_context == "counter_trend":
            risk_factors += 1
        if earnings.get("days_until") is not None and earnings["days_until"] <= 10:
            risk_factors += 1
        if ev["win_rate"] < 50:
            risk_factors += 1
        risk_map = {0: "LOW", 1: "MODERATE", 2: "MODERATE", 3: "HIGH"}
        risk_label = risk_map.get(risk_factors, "VERY HIGH")

        # Cautions
        cautions = []
        if _sector_skip:
            cautions.append(f"Sector {'lagging' if _sector_status == 'lagging' else 'leading'} — scan would skip")
        if not mkt_aligned:
            cautions.append("Market disagrees")
        if setup_context == "counter_trend":
            cautions.append("Counter-trend")
        if not trend_agrees:
            cautions.append("Against 200 MA")
        if vol_ratio < 1.0:
            cautions.append(f"Weak volume ({vol_ratio:.1f}x)")
        if earnings.get("days_until") is not None and earnings["days_until"] <= 10:
            cautions.append(f"Earnings in {earnings['days_until']}d")
        if _opp_list:
            opp_names = [s["indicator"] for s in _opp_list]
            cautions.append(f"Opposing: {', '.join(opp_names)}")

        # ==============================================
        #   THE TRADE CARD
        # ==============================================
        L.append("")
        L.append("  " + "-" * 60)
        L.append(f"    {tag} — {play_name}")
        L.append("  " + "-" * 60)
        if _used_close_fallback:
            L.append(f"    ** Live price ${price:.2f} vs close ${close_price:.2f} ({(price-close_price)/close_price*100:+.1f}%) — using close-price signals")
        L.append("")
        L.append(f"    Tier:      {tier}")
        L.append(f"    Entry:     ${price:.2f}")
        L.append(f"    Stop:      ${stop:.2f}  (${risk:.2f} risk/share)")
        L.append(f"    TP1:       ${tp1:.2f}  (1R)")
        L.append(f"    TP2:       ${tp2:.2f}  (2R)")
        L.append(f"    TP3:       ${tp3:.2f}  (3R)")
        L.append(f"    R:R:       1:{rr:.1f} to TP2")
        L.append(f"    Score:     {score:.1f}/10  |  Win Rate: {ev['win_rate']:.0f}%  |  EV: {ev['ev_ratio']:+.2f}R")
        _kf = kelly_fraction(ev["win_rate"], ev.get("avg_win", 0), ev.get("avg_loss", 0))
        if _kf > 0:
            L.append(f"    Kelly:     {_kf*100:.2f}% of account  (quarter-Kelly)")
        if _intel_total != 0:
            _iparts = [f"{k} {v:+.2f}" for k, v in _intel_adj.items() if k != "total" and v != 0]
            L.append(f"    Intel:     {_intel_total:+.2f} pts  ({', '.join(_iparts)})")
        L.append(f"    Risk:      {risk_label}" + (f"  |  Value: {_val_str}" if _val_str else ""))
        L.append("")

        # ==============================================
        #   PLAY — why this setup
        # ==============================================
        L.append(f"    PLAY: {play_name}")
        # Build narrative from the primary signal that defines the play
        primary_sig = next((s for s in sig_list if s["indicator"].lower().replace(" ", "_") == play_type
                            or s["indicator"].lower().replace(" ", "") == play_type.replace("_", "")),
                           None)
        if primary_sig and primary_sig.get("meta"):
            L.append(f"      {_explain_signal(primary_sig)}")
        elif primary_sig:
            L.append(f"      {primary_sig['detail']}")
        else:
            # Play is a composite — explain the combination
            sig_names = [s["indicator"] for s in sig_list]
            L.append(f"      Multiple signals converging: {', '.join(sig_names)}.")
        other_tags = [t for t in play_tags if t != play_type][:3]
        if other_tags:
            other_plays = [PLAY_TYPES.get(t, t) for t in other_tags]
            L.append(f"      Also tagged: {', '.join(other_plays)}")
        L.append("")

        # ==============================================
        #   SIGNALS — thorough explanation of each
        # ==============================================
        L.append(f"    SIGNALS ({len(sig_list)}):")
        for s in sig_list:
            wt = SIGNAL_WEIGHTS.get(s["indicator"], 0.5)
            explanation = _explain_signal(s)
            L.append(f"      [{wt:.2f}] {s['indicator']}")
            # Wrap explanation to ~70 chars per line, indented
            words = explanation.split()
            line = "            "
            for w in words:
                if len(line) + len(w) + 1 > 76:
                    L.append(line.rstrip())
                    line = "            " + w + " "
                else:
                    line += w + " "
            if line.strip():
                L.append(line.rstrip())
        L.append("")

        # ==============================================
        #   INDICATORS — compact summary
        # ==============================================
        L.append("    INDICATORS:")
        L.append(f"      RSI: {rsi_val:.0f} ({rsi_zone})  |  Stoch: %K={stoch_k_val:.0f} ({stoch_zone})  |  ADX: {adx_val:.0f} ({adx_label})")
        L.append(f"      MACD: {macd_cross}, hist {hist_dir}  |  Vol: {vol_ratio:.2f}x avg  |  ATR: ${atr_val:.2f} ({atr_pct:.1f}%)")
        L.append("")

        # ==============================================
        #   SUPPORT / RESISTANCE
        # ==============================================
        L.append(f"    Support:   ${support:.2f}  ({(price - support)/price*100:+.1f}%)")
        L.append(f"    Resist:    ${resistance:.2f}  ({(resistance - price)/price*100:+.1f}%)")
        L.append("")

        # ==============================================
        #   CONTEXT — trend alignment, macro picture
        # ==============================================
        L.append("    CONTEXT:")
        # Trend alignment
        _ma200_val = calculate_200ma(df).iloc[-1]
        ma_pos = ""
        if not pd.isna(_ma200_val):
            ma_dist = (price - float(_ma200_val)) / float(_ma200_val) * 100
            ma_pos = f" ({ma_dist:+.1f}% from 200 MA)"
        L.append(f"      Daily: {trend_200.upper()}{ma_pos}  |  Weekly: {htf_trend.upper()}")
        if rs_raw is not None:
            rs_label = "outperforming" if rs_raw > 0 else "underperforming"
            L.append(f"      RS vs SPY: {rs_raw:+.1f}% ({rs_label})")
        if abs(chg_1d) > 8 or abs(chg_5d) > 15:
            L.append(f"      !! UNUSUAL MOVE — check NEWS section for catalyst")
        L.append(f"      Price action: 1d {chg_1d:+.1f}%  5d {chg_5d:+.1f}%  20d {chg_20d:+.1f}%  60d {chg_60d:+.1f}%")
        L.append(f"      52w range: ${low_52w:.2f} — ${high_52w:.2f}  (currently {off_high:+.1f}% from high)")
        if earnings.get("days_until") is not None:
            earn_warn = "  !! IMMINENT" if earnings["days_until"] <= 5 else ""
            L.append(f"      Earnings: {earnings['days_until']}d away ({earnings.get('date', '?')}){earn_warn}")
        if earnings.get("days_since") is not None:
            L.append(f"      Recent earnings: {earnings['days_since']}d ago ({earnings.get('last_date', '?')}) — post-earnings price action may dominate technicals")

        # Cautions
        if cautions:
            L.append(f"      Cautions: {' | '.join(cautions)}")
        L.append("")

        # ==============================================
        #   FUNDAMENTALS
        # ==============================================
        if fund and _dir_val.get("grade") and _dir_val["grade"] != "N/A":
            L.append("    FUNDAMENTALS:")
            L.append(f"      Value Grade: {_dir_val['grade']} ({_dir_val['score']}/100)")
            _sub_parts = []
            for label, key in [("Val", "valuation"), ("Profit", "profitability"),
                                ("Health", "health"), ("Growth", "growth")]:
                sc = _dir_val.get(key)
                if sc is not None:
                    _sub_parts.append(f"{label}: {sc}/25")
            if _sub_parts:
                L.append(f"      {' | '.join(_sub_parts)}")
            # Key raw fundamentals
            _raw = []
            if fund.get("trailing_pe") is not None:
                _raw.append(f"P/E: {fund['trailing_pe']:.1f}")
            if fund.get("peg_ratio") is not None:
                _raw.append(f"PEG: {fund['peg_ratio']:.2f}")
            if fund.get("profit_margins") is not None:
                _raw.append(f"Margins: {fund['profit_margins']*100:.1f}%")
            if fund.get("debt_to_equity") is not None:
                _raw.append(f"D/E: {fund['debt_to_equity']:.0f}%")
            if fund.get("return_on_equity") is not None:
                _raw.append(f"ROE: {fund['return_on_equity']*100:.1f}%")
            if fund.get("revenue_growth") is not None:
                _raw.append(f"Rev Growth: {fund['revenue_growth']*100:+.1f}%")
            if fund.get("free_cashflow") is not None:
                _fcf = fund["free_cashflow"]
                if abs(_fcf) >= 1e9:
                    _raw.append(f"FCF: ${_fcf/1e9:.1f}B")
                elif abs(_fcf) >= 1e6:
                    _raw.append(f"FCF: ${_fcf/1e6:.0f}M")
            if _raw:
                mid = (len(_raw) + 1) // 2
                L.append(f"      {' | '.join(_raw[:mid])}")
                if len(_raw) > mid:
                    L.append(f"      {' | '.join(_raw[mid:])}")
            L.append("")

        setups.append({
            "ticker": ticker,
            "direction": direction,
            "play_type": play_type,
            "play_tags": play_tags,
            "signals": [dict(s) for s in sig_list],
            "setup_context": setup_context,
            "sector_status": _sector_status,
            "score": score,
            "tier": tier,
            "ev": ev,
        })

    if not has_setup:
        L.append("")
        L.append("    No qualifying setups (need 3+ signals in one direction)")
        L.append("")

        # Still show a snapshot even without a setup
        L.append("    SNAPSHOT:")
        L.append(f"      RSI: {rsi_val:.0f} ({rsi_zone})  |  Stoch: %K={stoch_k_val:.0f} ({stoch_zone})  |  ADX: {adx_val:.0f} ({adx_label})")
        L.append(f"      MACD: {macd_cross}  |  Vol: {vol_ratio:.2f}x avg")
        L.append(f"      Support: ${support:.2f}  |  Resist: ${resistance:.2f}")
        L.append(f"      Daily: {trend_200.upper()}  |  Weekly: {htf_trend.upper()}")
        L.append(f"      Price action: 1d {chg_1d:+.1f}%  5d {chg_5d:+.1f}%  20d {chg_20d:+.1f}%")
        L.append(f"      52w: ${low_52w:.2f} — ${high_52w:.2f}  ({off_high:+.1f}% from high)")
        if bull_signals:
            L.append(f"      Bull signals ({len(bull_signals)}): {', '.join(s['indicator'] for s in bull_signals)}")
        if bear_signals:
            L.append(f"      Bear signals ({len(bear_signals)}): {', '.join(s['indicator'] for s in bear_signals)}")
        L.append("")

    # ==============================================
    #   MARKET INTELLIGENCE
    # ==============================================
    _has_intel = any(v is not None for v in _intel.values())
    if _has_intel:
        L.append("    MARKET INTELLIGENCE:")

        _si = _intel.get("short_pct_float")
        _sr = _intel.get("short_ratio")
        if _si is not None:
            si_pct = _si * 100
            si_label = ("EXTREME" if si_pct > 20 else "HIGH" if si_pct > 10
                       else "MODERATE" if si_pct > 5 else "LOW")
            si_line = f"      Short Interest: {si_pct:.1f}% of float ({si_label})"
            if _sr is not None:
                si_line += f"  |  Days to Cover: {_sr:.1f}"
            L.append(si_line)

        _flt = _intel.get("float_shares")
        _out = _intel.get("shares_outstanding")
        if _flt is not None:
            if _flt >= 1e9:
                flt_str = f"{_flt/1e9:.1f}B"
            elif _flt >= 1e6:
                flt_str = f"{_flt/1e6:.0f}M"
            else:
                flt_str = f"{_flt/1e3:.0f}K"
            flt_label = ("MICRO" if _flt < 20e6 else "SMALL" if _flt < 50e6
                        else "MID" if _flt < 200e6 else "LARGE" if _flt < 1e9
                        else "MEGA")
            flt_line = f"      Float: {flt_str} shares ({flt_label})"
            if _out and _out > 0:
                flt_pct = _flt / _out * 100
                flt_line += f"  |  {flt_pct:.0f}% of outstanding"
            L.append(flt_line)

        _target = _intel.get("target_mean_price")
        _n_analysts = _intel.get("analyst_count")
        _rec = _intel.get("recommendation")
        if _target and _n_analysts and _n_analysts >= 1:
            upside = (_target - price) / price * 100 if price > 0 else 0
            sign = "+" if upside >= 0 else ""
            t_line = f"      Analysts: ${_target:.2f} target ({sign}{upside:.1f}%)"
            t_line += f"  |  {_n_analysts} analysts"
            if _rec:
                t_line += f"  |  {_rec.upper()}"
            L.append(t_line)

        _inst = _intel.get("inst_pct")
        _insdr = _intel.get("insider_pct")
        if _inst is not None:
            inst_pct = _inst * 100
            inst_label = ("HIGH" if inst_pct > 80 else "MODERATE" if inst_pct > 50
                         else "LOW" if inst_pct > 15 else "VERY LOW")
            inst_line = f"      Institutions: {inst_pct:.0f}% ({inst_label})"
            if _insdr is not None:
                inst_line += f"  |  Insiders: {_insdr*100:.1f}%"
            L.append(inst_line)

        _beta = _intel.get("beta")
        if _beta is not None:
            beta_label = ("HIGH BETA" if _beta > 1.5 else "ABOVE AVG" if _beta > 1.2
                         else "MARKET" if _beta > 0.8 else "LOW BETA" if _beta > 0.5
                         else "DEFENSIVE")
            L.append(f"      Beta: {_beta:.2f} ({beta_label})")

        L.append("")

    # ==============================================
    #   NEWS HEADLINES (once per ticker, outside setup loop)
    # ==============================================
    _yf_ticker = yf.Ticker(ticker)
    try:
        news_items = _yf_ticker.news or []
        if news_items:
            L.append("    NEWS:")
            shown = 0
            for item in news_items[:8]:
                # yfinance 1.1+ nests under "content"; older versions use flat keys
                content = item.get("content", item)
                title = content.get("title", "")
                if not title:
                    continue
                publisher = content.get("publisher", content.get("provider", {}).get("displayName", ""))
                pub_ts = content.get("providerPublishTime") or content.get("pubDate")
                if pub_ts:
                    try:
                        if isinstance(pub_ts, str):
                            pub_date = pub_ts[:10]  # ISO date string
                        else:
                            pub_date = datetime.fromtimestamp(pub_ts).strftime("%b %d")
                    except Exception:
                        pub_date = "?"
                else:
                    pub_date = "?"
                suffix = f" — {publisher}" if publisher else ""
                L.append(f"      [{pub_date}] {title}{suffix}")
                shown += 1
                if shown >= 5:
                    break
            if shown == 0:
                L.pop()  # remove the "NEWS:" header if nothing to show
            else:
                L.append("")
    except Exception:
        pass

    # ==============================================
    #   INSIDER ACTIVITY (once per ticker)
    # ==============================================
    try:
        insider_df = _yf_ticker.insider_transactions
        if insider_df is not None and not insider_df.empty:
            buys, sells = 0, 0
            buy_val, sell_val = 0.0, 0.0
            latest_text = ""
            for _, row in insider_df.head(20).iterrows():
                text = str(row.get("Text", "")).lower()
                value = abs(row.get("Value", 0) or 0)
                if "sale" in text or "sell" in text:
                    sells += 1
                    sell_val += value
                elif "purchase" in text or "buy" in text:
                    buys += 1
                    buy_val += value
                if not latest_text and value > 0:
                    start_date = row.get("Start Date", "")
                    if hasattr(start_date, "strftime"):
                        start_date = start_date.strftime("%b %d")
                    latest_text = f"{row.get('Text', 'Transaction')} on {start_date}"
            if buys + sells > 0:
                def _fmt_val(v):
                    if v >= 1e9: return f"${v/1e9:.1f}B"
                    if v >= 1e6: return f"${v/1e6:.1f}M"
                    if v >= 1e3: return f"${v/1e3:.0f}K"
                    return f"${v:.0f}"
                L.append("    INSIDERS:")
                L.append(f"      {sells} sells ({_fmt_val(sell_val)}) | {buys} buys ({_fmt_val(buy_val)})")
                if latest_text:
                    L.append(f"      Latest: {latest_text}")
                L.append("")
    except Exception:
        pass

    L.append("  " + "=" * 60)
    L.append("")

    report_text = "\n".join(L)

    # Save to file
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = REPORTS_DIR / f"deep_dive_{ticker}_{timestamp}.txt"
    path.write_text(report_text, encoding="utf-8")

    return report_text, path, setups


def _run_deep_dives(tickers: list, spy_df: pd.DataFrame = None,
                    regime: str = "unknown", market_trends: dict = None,
                    sector_rotation: dict = None, adaptive_weights: dict = None,
                    pillar_mults: dict = None, adaptive_wr: dict = None,
                    user_prefs: dict = None, rotation_divergence: dict = None,
                    distrib_threshold: float = None):
    """Run deep-dive reports on a list of tickers, with optional favorite marking."""
    all_setups = []
    for t in tickers:
        print(f"\n  Generating deep dive for {t}...")
        text, path, setups = _deep_dive(t, spy_df, regime=regime,
                                         market_trends=market_trends,
                                         sector_rotation=sector_rotation,
                                         adaptive_weights=adaptive_weights,
                                         pillar_mults=pillar_mults,
                                         adaptive_wr=adaptive_wr,
                                         user_prefs=user_prefs,
                                         rotation_divergence=rotation_divergence,
                                         distrib_threshold=distrib_threshold)
        print(text)
        if path:
            print(f"  Saved: {path}")
        all_setups.extend(setups)

    # Offer favorite marking after viewing deep dives
    if all_setups:
        fav_choices = [
            questionary.Choice(
                f"{s['ticker']:6s}  {'BULL' if s['direction'] == 'bullish' else 'BEAR'}  "
                f"{s['score']:.1f}/10  [{PLAY_TYPES.get(s.get('play_type', ''), '')}]",
                value=i,
            )
            for i, s in enumerate(all_setups)
        ]
        selected = questionary.checkbox(
            "Mark any as favorites? (Enter to skip)", choices=fav_choices
        ).ask()
        if selected:
            fav_setups = [all_setups[i] for i in selected]
            record_favorite_setups(fav_setups)
            fav_tickers = [all_setups[i]["ticker"] for i in selected]
            mark_picks_favorited(fav_tickers)
            print(f"  Recorded {len(selected)} favorites for preference learning")


# ---------------------------------------------------------------------------
# Post-scan interactive menu
# ---------------------------------------------------------------------------

def _fallback_menu(labels: list[str], values: list[str]) -> str | None:
    """Simple numbered menu fallback when questionary can't render."""
    print("\n  What next?")
    for i, label in enumerate(labels, 1):
        print(f"    {i}) {label}")
    try:
        raw = input("  Choice [number]: ").strip()
        if not raw:
            return None
        idx = int(raw) - 1
        if 0 <= idx < len(values):
            return values[idx]
    except (ValueError, EOFError, KeyboardInterrupt):
        pass
    return None


def _post_scan_menu(results: list, spy_df: pd.DataFrame = None,
                    regime: str = "unknown", market_trends: dict = None,
                    sector_rotation: dict = None, adaptive_weights: dict = None,
                    pillar_mults: dict = None, adaptive_wr: dict = None,
                    user_prefs: dict = None, rotation_divergence: dict = None,
                    distrib_threshold: float = None) -> str:
    """Show menu after scan completes. Returns action taken."""
    a_tiers = [r for r in results if r["tier"] == "A"]
    b_tiers = [r for r in results if r["tier"] == "B"]

    labels, values = [], []
    if results:
        labels.append(f"Mark favorites ({len(results)} picks)")
        values.append("favorites")
        labels.append("Exclude picks from learning")
        values.append("exclude")
    _curr_favs = load_favorites()
    _curr_alerts = load_alerts()
    fav_label = f"Manage watchlist ({len(_curr_favs)} favorites)" if _curr_favs else "Manage watchlist"
    alert_label = f"Manage price alerts ({len(_curr_alerts)})" if _curr_alerts else "Manage price alerts"
    labels.append(fav_label)
    values.append("watchlist")
    labels.append(alert_label)
    values.append("alerts")
    if a_tiers:
        a_names = ", ".join(r["ticker"] for r in a_tiers[:8])
        labels.append(f"Deep dive A-tiers ({len(a_tiers)}: {a_names})")
        values.append("a_tiers")
    if b_tiers:
        b_names = ", ".join(r["ticker"] for r in b_tiers[:8])
        labels.append(f"Deep dive B-tiers ({len(b_tiers)}: {b_names})")
        values.append("b_tiers")
    labels.append("Deep dive a specific ticker")
    values.append("custom")
    labels.append("Rescan now")
    values.append("rescan")
    labels.append("Sleep and rescan on schedule")
    values.append("sleep")
    labels.append("Exit")
    values.append("exit")

    # Visual separator so menu doesn't get lost in report output
    print("\n" + "=" * 60)
    print("  POST-SCAN MENU")
    print("=" * 60)

    # Try questionary first, fall back to simple numbered menu
    try:
        choices = [questionary.Choice(l, value=v) for l, v in zip(labels, values)]
        action = questionary.select("What next?", choices=choices).ask()
    except Exception:
        action = None

    if action is None:
        action = _fallback_menu(labels, values)
    if action is None:
        return "exit"

    if action == "favorites":
        try:
            pick_choices = [
                questionary.Choice(
                    f"{r['ticker']:6s}  {r['tier']}-TIER  {r['score']:.1f}/10  [{PLAY_TYPES.get(r.get('play_type', ''), '')}]",
                    value=r["ticker"],
                )
                for r in results
            ]
            selected = questionary.checkbox("Select your favorites:", choices=pick_choices).ask()
        except Exception:
            selected = None
        if selected:
            record_favorites(results, selected)
            mark_picks_favorited(selected)
            print(f"  Recorded {len(selected)} favorites for preference learning")
    elif action == "exclude":
        try:
            pick_choices = [
                questionary.Choice(
                    f"{r['ticker']:6s}  {r['tier']}-TIER  {r['score']:.1f}/10  [{PLAY_TYPES.get(r.get('play_type', ''), '')}]",
                    value=r["ticker"],
                )
                for r in results
            ]
            selected = questionary.checkbox("Select picks to exclude from adaptive learning:", choices=pick_choices).ask()
        except Exception:
            selected = None
        if not selected:
            # Fallback: manual input
            try:
                raw = input("  Ticker(s) to exclude (comma-separated): ").strip()
            except Exception:
                raw = ""
            if raw:
                selected = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if selected:
            try:
                reason = questionary.text("Reason (optional, e.g. 'AI fear selloff'):").ask()
            except Exception:
                reason = input("  Reason (optional): ").strip()
            mark_picks_excluded(selected, reason=reason or "")
            print(f"  Excluded {len(selected)} picks — their outcomes won't affect adaptive weights")
    elif action == "watchlist":
        _manage_watchlist()
    elif action == "alerts":
        _manage_alerts()
    elif action in ("a_tiers", "b_tiers", "custom"):
        _dd_ctx = dict(spy_df=spy_df, regime=regime, market_trends=market_trends,
                       sector_rotation=sector_rotation, adaptive_weights=adaptive_weights,
                       pillar_mults=pillar_mults, adaptive_wr=adaptive_wr,
                       user_prefs=user_prefs, rotation_divergence=rotation_divergence,
                       distrib_threshold=distrib_threshold)
        if action == "a_tiers":
            _run_deep_dives([r["ticker"] for r in a_tiers], **_dd_ctx)
        elif action == "b_tiers":
            _run_deep_dives([r["ticker"] for r in b_tiers], **_dd_ctx)
        else:
            try:
                raw = questionary.text("Ticker(s) to deep dive (comma-separated):").ask()
            except Exception:
                raw = input("  Ticker(s) to deep dive (comma-separated): ").strip()
            if raw:
                tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
                _run_deep_dives(tickers, **_dd_ctx)

    return action


def _quick_context():
    """Lightweight market context for deep dives without a full scan.
    Fetches SPY, regime, sector rotation, and cached adaptive values."""
    config = load_config()
    print("  Loading market context...")
    regime_info = detect_market_regime(config)
    regime = regime_info["regime"]
    spy_df = regime_info["spy_df"]
    print(f"  Regime: {regime.upper()} (ADX {regime_info['adx']:.1f})")
    market_trends = get_market_trend(config, prefetched={"SPY": spy_df})
    sector_rotation = calculate_sector_rotation(spy_df)
    cached = get_cached_adaptive_values(regime)
    user_prefs = load_user_preferences()
    return {
        "spy_df": spy_df,
        "regime": regime,
        "market_trends": market_trends,
        "sector_rotation": sector_rotation,
        "adaptive_weights": cached["weights"],
        "pillar_mults": cached["pillars"],
        "adaptive_wr": cached["win_rates"],
        "user_prefs": user_prefs,
        "rotation_divergence": None,
        "distrib_threshold": cached["distrib_thresh"],
    }


def _manage_watchlist():
    """Interactive add/remove for favorites watchlist."""
    _wl_favs = load_favorites()
    if _wl_favs:
        print(f"  Watchlist ({len(_wl_favs)}): {', '.join(sorted(_wl_favs))}")
    else:
        print("  Watchlist is empty")
    try:
        wl_action = questionary.select("Watchlist action:", choices=[
            questionary.Choice("Add tickers", value="add"),
            questionary.Choice("Remove tickers", value="remove"),
            questionary.Choice("Back", value="back"),
        ]).ask()
    except Exception:
        wl_action = _fallback_menu(["Add tickers", "Remove tickers", "Back"],
                                   ["add", "remove", "back"])
    if wl_action == "add":
        while True:
            _wl_favs = load_favorites()
            if _wl_favs:
                print(f"  Watchlist ({len(_wl_favs)}): {', '.join(sorted(_wl_favs))}")
            try:
                raw = questionary.text("Ticker to add (or 'done'):").ask()
            except Exception:
                raw = input("  Ticker to add (or 'done'): ").strip()
            if not raw or raw.strip().lower() == "done":
                break
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
            added = add_favorites(tickers)
            if added:
                print(f"  Added: {', '.join(added)}")
            else:
                print("  Already on watchlist")
    elif wl_action == "remove":
        while True:
            _wl_favs = load_favorites()
            if not _wl_favs:
                print("  Watchlist is empty")
                break
            print(f"  Watchlist ({len(_wl_favs)}): {', '.join(sorted(_wl_favs))}")
            try:
                raw = questionary.text("Ticker to remove (or 'done'):").ask()
            except Exception:
                raw = input("  Ticker to remove (or 'done'): ").strip()
            if not raw or raw.strip().lower() == "done":
                break
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
            removed = remove_favorites(tickers)
            if removed:
                print(f"  Removed: {', '.join(removed)}")
            else:
                print("  Not on watchlist")


def _manage_alerts():
    """Interactive add/remove for price alerts."""
    alerts = load_alerts()
    if alerts:
        print(f"  Price alerts ({len(alerts)}):")
        for t in sorted(alerts):
            a = alerts[t]
            print(f"    {t:6s} {a['direction']:5s} ${a['price']:.2f}")
    else:
        print("  No price alerts set")
    try:
        al_action = questionary.select("Alerts action:", choices=[
            questionary.Choice("Add alert", value="add"),
            questionary.Choice("Remove alert", value="remove"),
            questionary.Choice("Back", value="back"),
        ]).ask()
    except Exception:
        al_action = _fallback_menu(["Add alert", "Remove alert", "Back"],
                                   ["add", "remove", "back"])
    if al_action == "add":
        while True:
            alerts = load_alerts()
            if alerts:
                print(f"  Alerts ({len(alerts)}): " + ", ".join(
                    f"{t} {alerts[t]['direction']} ${alerts[t]['price']:.2f}" for t in sorted(alerts)))

            try:
                raw = questionary.text("TICKER above/below PRICE (e.g. HALO below 67) or 'done':").ask()
            except Exception:
                raw = input("  TICKER above/below PRICE (e.g. HALO below 67) or 'done': ").strip()
            if not raw or raw.strip().lower() == "done":
                break
            parts = raw.strip().split()
            if len(parts) < 3:
                print("  Usage: TICKER above/below PRICE (e.g. HALO below 67, NVDA above 150)")
                continue
            ticker = parts[0].upper()
            direction = parts[1].lower()
            if direction not in ("above", "below"):
                print("  Direction must be 'above' or 'below'")
                continue
            try:
                price = float(parts[2].replace("$", ""))
            except ValueError:
                print(f"  Invalid price: {parts[2]}")
                continue
            if add_alert(ticker, price, direction):
                print(f"  Alert set: {ticker} {direction} ${price:.2f}")

            else:
                print("  Invalid ticker or price")
    elif al_action == "remove":
        while True:
            alerts = load_alerts()
            if not alerts:
                print("  No alerts to remove")
                break
            print(f"  Alerts ({len(alerts)}): " + ", ".join(f"{t} @ ${p:.2f}" for t, p in sorted(alerts.items())))
            try:
                raw = questionary.text("Ticker to remove (or 'done'):").ask()
            except Exception:
                raw = input("  Ticker to remove (or 'done'): ").strip()
            if not raw or raw.strip().lower() == "done":
                break
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
            for t in tickers:
                if remove_alert(t):
                    print(f"  Removed: {t}")
                else:
                    print(f"  {t} not in alerts")


def _startup_menu() -> str:
    """Show startup menu before running a scan."""
    _favs = load_favorites()
    _alerts = load_alerts()
    fav_label = f"Manage watchlist ({len(_favs)})" if _favs else "Manage watchlist"
    alert_label = f"Manage price alerts ({len(_alerts)})" if _alerts else "Manage price alerts"
    labels = [
        "Run full scan",
        "Deep dive a specific ticker",
        fav_label,
        alert_label,
        "Exit",
    ]
    values = ["scan", "deep_dive", "watchlist", "alerts", "exit"]

    try:
        choices = [questionary.Choice(l, value=v) for l, v in zip(labels, values)]
        action = questionary.select("What would you like to do?", choices=choices).ask()
    except Exception:
        action = None
    if action is None:
        action = _fallback_menu(labels, values)
    return action or "exit"


def main():
    print("=" * 60)
    print("  PROFESSIONAL STOCK ALERT SYSTEM")
    print("  Weighted Scoring Engine v7 — Self-Learning")
    print("=" * 60)

    config = load_config()
    print(f"  Tickers:    {len(config['tickers'])}")
    print(f"  Interval:   {config['check_interval_minutes']} min")
    print(f"  Reports:    {REPORTS_DIR}")
    print("=" * 60)

    action = _startup_menu()

    while action in ("watchlist", "alerts"):
        if action == "watchlist":
            _manage_watchlist()
        else:
            _manage_alerts()
        action = _startup_menu()

    if action == "exit":
        print("  Shutting down.")
        return

    results = []
    if action == "scan":
        results = run_check(config)
    elif action == "deep_dive":
        ctx = _quick_context()
        try:
            raw = questionary.text("Ticker(s) to deep dive (comma-separated):").ask()
        except Exception:
            raw = input("  Ticker(s) to deep dive (comma-separated): ").strip()
        if raw:
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
            _run_deep_dives(tickers, **ctx)

    while True:
        _ctx = {
            "spy_df": getattr(run_check, '_last_spy_df', None),
            "regime": getattr(run_check, '_last_regime', "unknown"),
            "market_trends": getattr(run_check, '_last_market_trends', None),
            "sector_rotation": getattr(run_check, '_last_sector_rotation', None),
            "adaptive_weights": getattr(run_check, '_last_adaptive_wts', None),
            "pillar_mults": getattr(run_check, '_last_pillar_m', None),
            "adaptive_wr": getattr(run_check, '_last_adaptive_wr', None),
            "user_prefs": getattr(run_check, '_last_user_prefs', None),
            "rotation_divergence": getattr(run_check, '_last_rot_divergence', None),
            "distrib_threshold": getattr(run_check, '_last_distrib_thresh', None),
        }
        # If we came from deep_dive without a scan, use quick context
        if not results and action == "deep_dive":
            _ctx = ctx
        action = _post_scan_menu(results, **_ctx)

        if action == "exit":
            print("  Shutting down.")
            break
        elif action == "rescan":
            config = load_config()
            results = run_check(config)
        elif action == "sleep":
            interval = config["check_interval_minutes"]
            print(f"\n  Sleeping {interval} minutes...")
            time.sleep(interval * 60)
            config = load_config()
            results = run_check(config)
        # a_tiers, b_tiers, custom -> loop back to menu


if __name__ == "__main__":
    main()
