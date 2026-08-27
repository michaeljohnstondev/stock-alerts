import json
from collections import Counter
from datetime import datetime, date

import pandas as pd

from .constants import PICKS_HISTORY_FILE, MISSED_MOVERS_FILE, REPORTS_DIR, TRACK_DAYS, SCRATCH_THRESHOLD, PLAY_TYPES
from .data import fetch_stock_data, fetch_intraday_data


# ---------------------------------------------------------------------------
# Performance tracking
# ---------------------------------------------------------------------------

def _calc_analyst_upside(result: dict):
    """Calculate analyst upside % from intel data for bucketing."""
    intel = result.get("intel", {})
    target = intel.get("target_mean_price")
    price = result.get("price")
    n = intel.get("analyst_count")
    if target and price and n and n >= 3 and price > 0:
        return round((target - price) / price, 3)
    return None


def save_picks(results: list, regime: str = "unknown", regime_adx: float = 0):
    """Save ALL picks to history for performance tracking and adaptive learning."""
    history = {"picks": [], "stats": {"by_play_type": {}, "by_tier": {}, "overall": {}}}
    if PICKS_HISTORY_FILE.exists():
        try:
            history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    existing_active = {
        (p["ticker"], p["direction"])
        for p in history["picks"]
        if p.get("status") in ("open", "tracking", "partial")
    }

    today = date.today().isoformat()
    for r in results:
        if r["tier"] == "C":
            continue  # C-tier = watchlist only, not tracked
        if r.get("earnings_recent"):
            continue  # Earnings within 7d — show in report but don't track
        if r.get("trade_type", "").startswith("WATCH"):
            continue  # WATCH = no intraday data to manage risk, don't track
        key = (r["ticker"], r["direction"])
        if key in existing_active:
            continue
        plan = r.get("plan", {})
        history["picks"].append({
            "ticker": r["ticker"],
            "play_type": r.get("play_type", "multi_signal"),
            "play_tags": r.get("play_tags", [r.get("play_type", "multi_signal")]),
            "setup_context": r.get("setup_context", "with_trend"),
            "signal_indicators": [s["indicator"] for s in r.get("signals", [])
                                  if s.get("direction") == r["direction"]],
            "distrib_vol_ratio": next(
                (s.get("meta", {}).get("vol_ratio")
                 for s in r.get("signals", [])
                 if s.get("indicator") in ("Distribution", "Early Distribution", "Dist Breakdown")
                 and s.get("direction") == r["direction"]
                 and s.get("meta", {}).get("vol_ratio") is not None),
                None),
            "direction": r["direction"],
            "entry": plan.get("entry", r["price"]),
            "stop": plan.get("stop", 0),
            "tp1": plan.get("tp1", 0),
            "tp2": plan.get("tp2", 0),
            "score": r["score"],
            "breakdown": r.get("breakdown", {}),
            "tier": r["tier"],
            "trade_type": r["trade_type"],
            "sector": r.get("sector", "Unknown"),
            "sector_status": r.get("sector_status"),
            "rs_raw": r.get("rs_raw"),
            "vol_ratio": r.get("vol_ratio"),
            "regime": regime,
            "regime_adx": round(regime_adx, 1),
            "ev_ratio": r.get("ev", {}).get("ev_ratio", 0),
            "win_rate_est": r.get("ev", {}).get("win_rate", 0),
            # Market intelligence (for adaptive learning)
            "short_pct_float": r.get("intel", {}).get("short_pct_float"),
            "float_shares": r.get("intel", {}).get("float_shares"),
            "analyst_upside_pct": _calc_analyst_upside(r),
            "inst_pct": r.get("intel", {}).get("inst_pct"),
            "beta": r.get("intel", {}).get("beta"),
            "scan_date": today,
            "scan_timestamp": datetime.now().isoformat(),
            "status": "open",
            "result_1d": None,
            "result_5d": None,
            "result_10d": None,
            "exit_price_1d": None,
            "exit_price_5d": None,
            "exit_price_10d": None,
            "high_since": None,
            "low_since": None,
        })

    PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def mark_picks_favorited(selected_tickers: list, direction_map: dict = None):
    """Flag active picks as favorited (doubles adaptive learning weight).
    Called after user marks favorites in the post-scan menu.
    direction_map: optional {ticker: direction} for exact matching."""
    if not selected_tickers or not PICKS_HISTORY_FILE.exists():
        return
    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return

    selected_set = set(selected_tickers)
    today = date.today().isoformat()
    for p in history.get("picks", []):
        if p["ticker"] not in selected_set:
            continue
        if p.get("status") in ("scored",):
            continue  # already consumed, don't bother
        if direction_map and p.get("direction") != direction_map.get(p["ticker"]):
            continue
        # Only mark picks from recent scans (same day or yesterday)
        if p.get("scan_date", "") >= today[:8]:
            p["favorited"] = True

    PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def mark_picks_excluded(selected_tickers: list, reason: str = ""):
    """Flag active picks as excluded (zero adaptive learning weight).
    Excluded picks are still scored and tracked, but their outcomes
    don't affect signal weights, win rates, or any adaptive variables.
    Use for 'act of God' events (news-driven crashes, etc.)."""
    if not selected_tickers or not PICKS_HISTORY_FILE.exists():
        return
    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return

    selected_set = set(selected_tickers)
    count = 0
    for p in history.get("picks", []):
        if p["ticker"] not in selected_set:
            continue
        if p.get("excluded"):
            continue
        p["excluded"] = True
        if reason:
            p["exclude_reason"] = reason
        count += 1

    if count > 0:
        PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def repair_signal_indicators():
    """One-time fix: bearish picks had bullish signal names stored (Failed Breakdown
    instead of Failed Breakout, Double Bottom instead of Double Top).  Corrects
    signal_indicators to match the pick direction, using play_tags as the source
    of truth since those were always correct."""
    if not PICKS_HISTORY_FILE.exists():
        return 0
    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0

    # Map: bullish signal name → bearish counterpart (and vice versa)
    swap_map = {
        "Failed Breakdown": "Failed Breakout",
        "Failed Breakout": "Failed Breakdown",
        "Double Bottom": "Double Top",
        "Double Top": "Double Bottom",
        "M on Uptrend": "W on Downtrend",
        "W on Downtrend": "M on Uptrend",
    }

    picks = history.get("picks", []) if isinstance(history, dict) else history
    fixed = 0
    for pick in picks:
        direction = pick.get("direction", "")
        sigs = pick.get("signal_indicators", [])
        if not sigs or not direction:
            continue

        new_sigs = []
        changed = False
        for sig in sigs:
            if sig in swap_map:
                # Check if this signal name is wrong for the direction
                # Failed Breakdown is always bullish; on a bearish pick it's wrong
                bullish_names = {"Failed Breakdown", "Double Bottom", "M on Uptrend"}
                bearish_names = {"Failed Breakout", "Double Top", "W on Downtrend"}
                if direction == "bearish" and sig in bullish_names:
                    new_sigs.append(swap_map[sig])
                    changed = True
                elif direction == "bullish" and sig in bearish_names:
                    new_sigs.append(swap_map[sig])
                    changed = True
                else:
                    new_sigs.append(sig)
            else:
                new_sigs.append(sig)

        if changed:
            pick["signal_indicators"] = new_sigs
            fixed += 1

    if fixed:
        PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return fixed


def backfill_pick_fields(regime: str = "unknown", regime_adx: float = 0):
    """Backfill missing fields on old picks saved by earlier code versions.
    Ensures losers analysis, adaptive learning, and scorecard have complete data."""
    if not PICKS_HISTORY_FILE.exists():
        return 0
    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0

    picks = history.get("picks", []) if isinstance(history, dict) else history
    fixed = 0
    defaults = {
        "regime": regime,
        "regime_adx": round(regime_adx, 1),
        "vol_ratio": None,
        "breakdown": {},
        "play_tags": None,  # special — derived from play_type
        "setup_context": "with_trend",
        "signal_indicators": [],
        "ev_ratio": 0,
        "win_rate_est": 0,
        "sector_status": None,
        "result_1d": None,
        "exit_price_1d": None,
        "short_pct_float": None,
        "float_shares": None,
        "analyst_upside_pct": None,
        "inst_pct": None,
        "beta": None,
    }
    for pick in picks:
        changed = False
        for field, default in defaults.items():
            if field not in pick:
                if field == "play_tags":
                    pick[field] = [pick.get("play_type", "multi_signal")]
                else:
                    pick[field] = default
                changed = True
        if changed:
            fixed += 1

    if fixed:
        PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return fixed


def score_previous_picks() -> dict:
    """Score open picks from previous scans. Returns summary for report."""
    if not PICKS_HISTORY_FILE.exists():
        return {"has_data": False}

    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"has_data": False}

    picks = history.get("picks", [])
    if not picks:
        return {"has_data": False}

    today = date.today()
    updated = False

    for pick in picks:
        if pick["status"] not in ("open",):
            continue

        scan_date = date.fromisoformat(pick["scan_date"])
        days_elapsed = (today - scan_date).days

        if days_elapsed < 1:
            continue  # Same-day pick — nothing to score yet

        # Fetch price data since scan
        try:
            df = fetch_stock_data(pick["ticker"], period="1mo")
            if df.empty:
                continue
        except Exception:
            continue

        # Filter to days after scan (tz-aware to match yfinance index)
        scan_dt = pd.Timestamp(pick["scan_date"])
        if df.index.tz is not None:
            scan_dt = scan_dt.tz_localize(df.index.tz)
        after_scan = df[df.index > scan_dt]
        if after_scan.empty:
            continue

        # Actual trading days from real data (not approximate formula)
        trading_days_elapsed = len(after_scan)

        entry = pick["entry"]
        stop = pick["stop"]
        tp1 = pick["tp1"]
        is_long = pick["direction"] == "bullish"

        # Track high/low since entry
        pick["high_since"] = round(float(after_scan["High"].max()), 2)
        pick["low_since"] = round(float(after_scan["Low"].min()), 2)
        updated = True

        # Detect earnings-like gap during tracking window (5%+ overnight move).
        # If yfinance missed upcoming earnings, this catches the event retroactively
        # so the pick is excluded from adaptive learning (not a setup failure).
        if not pick.get("earnings_gap"):
            scan_day = df[df.index <= scan_dt]
            check_bars = after_scan.iloc[:min(10, len(after_scan))]
            for i in range(len(check_bars)):
                if i == 0:
                    prev_close = float(scan_day.iloc[-1]["Close"]) if len(scan_day) > 0 else None
                else:
                    prev_close = float(check_bars.iloc[i - 1]["Close"])
                if prev_close and prev_close > 0:
                    day_open = float(check_bars.iloc[i]["Open"])
                    gap_pct = abs(day_open - prev_close) / prev_close
                    if gap_pct >= 0.05:
                        pick["earnings_gap"] = True
                        break

        # Which windows apply to this pick?
        # 1d only for day trades — swing trade P&L after 1 day is noise
        is_day_trade = pick.get("trade_type", "").startswith("DAY TRADE")

        # Score at each window
        for window in TRACK_DAYS:
            if window == 1 and not is_day_trade:
                continue  # 1d only calibrates day trades

            result_key = f"result_{window}d"
            exit_key = f"exit_price_{window}d"

            if pick.get(result_key) is not None:
                continue  # Already scored

            # Use available data (up to window bars) — don't wait for full
            # window to elapse if TP1/stop was already hit in the data we have.
            window_data = after_scan.iloc[:min(window, len(after_scan))]

            if is_long:
                hit_tp1 = (window_data["High"] >= tp1).any()
                hit_stop = (window_data["Low"] <= stop).any()
            else:
                hit_tp1 = (window_data["Low"] <= tp1).any()
                hit_stop = (window_data["High"] >= stop).any()

            if hit_tp1 and hit_stop:
                # Both hit — find the conflict bar(s) and resolve
                _resolved = False
                for _bar_date, row in window_data.iterrows():
                    if is_long:
                        _bar_hit_stop = row["Low"] <= stop
                        _bar_hit_tp = row["High"] >= tp1
                    else:
                        _bar_hit_stop = row["High"] >= stop
                        _bar_hit_tp = row["Low"] <= tp1

                    if _bar_hit_stop and not _bar_hit_tp:
                        pick[result_key] = "loss"
                        pick[exit_key] = stop
                        _resolved = True
                        break
                    if _bar_hit_tp and not _bar_hit_stop:
                        pick[result_key] = "win"
                        pick[exit_key] = tp1
                        _resolved = True
                        break
                    if _bar_hit_stop and _bar_hit_tp:
                        # Same daily bar hit both — try intraday resolution
                        try:
                            intra = fetch_intraday_data(ticker)
                            if not intra.empty:
                                # Filter to the conflict date
                                _conflict_dt = _bar_date.date() if hasattr(_bar_date, 'date') else _bar_date
                                intra_day = intra[intra.index.date == _conflict_dt] if hasattr(intra.index, 'date') else intra
                                for _, irow in intra_day.iterrows():
                                    if is_long:
                                        if irow["Low"] <= stop:
                                            pick[result_key] = "loss"
                                            pick[exit_key] = stop
                                            _resolved = True
                                            break
                                        if irow["High"] >= tp1:
                                            pick[result_key] = "win"
                                            pick[exit_key] = tp1
                                            _resolved = True
                                            break
                                    else:
                                        if irow["High"] >= stop:
                                            pick[result_key] = "loss"
                                            pick[exit_key] = stop
                                            _resolved = True
                                            break
                                        if irow["Low"] <= tp1:
                                            pick[result_key] = "win"
                                            pick[exit_key] = tp1
                                            _resolved = True
                                            break
                        except Exception:
                            pass  # Fall through to daily heuristic
                        # Daily heuristic fallback: assume stop hit first (conservative)
                        if not _resolved:
                            pick[result_key] = "loss"
                            pick[exit_key] = stop
                            _resolved = True
                        break
                # Safety fallback
                if not _resolved:
                    pick[result_key] = "loss"
                    pick[exit_key] = stop
            elif hit_tp1:
                pick[result_key] = "win"
                pick[exit_key] = tp1
            elif hit_stop:
                pick[result_key] = "loss"
                pick[exit_key] = stop
            elif trading_days_elapsed >= window:
                # Window expired, neither hit — mark based on P&L vs scratch threshold
                # Must clear SCRATCH_THRESHOLD (0.5%) to count as win — opportunity cost
                current_price = float(after_scan["Close"].iloc[-1])
                pnl = (current_price - entry) / entry if is_long else (entry - current_price) / entry
                if pnl >= SCRATCH_THRESHOLD:
                    pick[result_key] = "win"
                elif pnl <= -SCRATCH_THRESHOLD:
                    pick[result_key] = "loss"
                else:
                    pick[result_key] = "draw"  # broke even — don't count for adaptive
                pick[exit_key] = round(current_price, 2)

        # Update status: check only applicable windows
        applicable = [w for w in TRACK_DAYS if w > 1 or is_day_trade]
        all_scored = all(pick.get(f"result_{w}d") is not None for w in applicable)
        any_scored = any(pick.get(f"result_{w}d") is not None for w in applicable)
        if all_scored:
            pick["status"] = "scored"
        elif any_scored:
            pick["status"] = "partial"

        # Near-miss tracking: how close did price get to TP1?
        high_since = pick.get("high_since")
        low_since = pick.get("low_since")
        if entry and tp1 and entry != tp1:
            if is_long and high_since:
                tp1_pct = (high_since - entry) / (tp1 - entry) if tp1 > entry else 0
            elif not is_long and low_since:
                tp1_pct = (entry - low_since) / (entry - tp1) if entry > tp1 else 0
            else:
                tp1_pct = 0
            pick["tp1_reached_pct"] = round(max(0, min(2.0, tp1_pct)), 3)

            # TP1 reached → upgrade any "loss" results to "win"
            # Rationale: if price hit our target, that's a winning setup
            # regardless of what happened afterward (trader should have
            # taken profit or trailed the stop at that point).
            if pick["tp1_reached_pct"] >= 1.0:
                for w in applicable:
                    rk = f"result_{w}d"
                    if pick.get(rk) == "loss":
                        pick[rk] = "win"
                        pick[f"exit_price_{w}d"] = tp1  # credit TP1 exit
                        pick["tp1_upgraded"] = True      # audit flag
                        pick.pop("logged_to_file", None) # re-log as winner

    # Rebuild stats
    stats = {"by_play_type": {}, "by_tier": {}, "by_signal": {}, "overall": {
        "wins_1d": 0, "losses_1d": 0, "wins_5d": 0, "losses_5d": 0,
        "wins_10d": 0, "losses_10d": 0, "total": 0
    }}

    for pick in picks:
        # Credit ALL matching play tags, not just the primary
        all_tags = pick.get("play_tags", [pick.get("play_type", "multi_signal")])
        tier = pick.get("tier", "C")
        sig_indicators = pick.get("signal_indicators", [])

        # C-tier: skip stats recording (not traded, noisy outcomes)
        if tier == "C":
            continue

        # Earnings gap: skip stats (not a setup failure, binary event)
        if pick.get("earnings_gap"):
            continue

        for pt in all_tags:
            if pt not in stats["by_play_type"]:
                stats["by_play_type"][pt] = {"wins_1d": 0, "losses_1d": 0, "wins_5d": 0, "losses_5d": 0, "wins_10d": 0, "losses_10d": 0, "total": 0}
            stats["by_play_type"][pt]["total"] += 1

        # Track signal-level stats for adaptive weights
        for sig in sig_indicators:
            if sig not in stats["by_signal"]:
                stats["by_signal"][sig] = {"wins_10d": 0, "losses_10d": 0, "total": 0}
            stats["by_signal"][sig]["total"] += 1

        if tier not in stats["by_tier"]:
            stats["by_tier"][tier] = {"wins_1d": 0, "losses_1d": 0, "wins_5d": 0, "losses_5d": 0, "wins_10d": 0, "losses_10d": 0, "total": 0}

        stats["by_tier"][tier]["total"] += 1
        stats["overall"]["total"] += 1

        for window in TRACK_DAYS:
            result_key = f"result_{window}d"
            r = pick.get(result_key)
            if r == "win":
                for pt in all_tags:
                    stats["by_play_type"][pt][f"wins_{window}d"] += 1
                if window == 10:
                    for sig in sig_indicators:
                        stats["by_signal"][sig]["wins_10d"] += 1
                stats["by_tier"][tier][f"wins_{window}d"] += 1
                stats["overall"][f"wins_{window}d"] += 1
            elif r == "loss":
                for pt in all_tags:
                    stats["by_play_type"][pt][f"losses_{window}d"] += 1
                if window == 10:
                    for sig in sig_indicators:
                        stats["by_signal"][sig]["losses_10d"] += 1
                stats["by_tier"][tier][f"losses_{window}d"] += 1
                stats["overall"][f"losses_{window}d"] += 1

    history["stats"] = stats

    if updated:
        PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")

    # Build recent picks list (last 10, A/B tier only)
    recent = []
    ab_picks = [p for p in picks if p.get("tier") in ("A", "B")]
    for pick in reversed(ab_picks[-10:]):
        entry = pick["entry"]
        if pick["high_since"] is not None:
            is_long = pick["direction"] == "bullish"
            current_est = pick["high_since"] if is_long else pick["low_since"]
            pnl_pct = ((current_est - entry) / entry * 100) if is_long else ((entry - current_est) / entry * 100)
        else:
            pnl_pct = 0
        days = (today - date.fromisoformat(pick["scan_date"])).days
        r10 = pick.get("result_10d")
        r5 = pick.get("result_5d")
        r1 = pick.get("result_1d")
        if r10 == "win":
            status_str = "WIN @TP1"
        elif r10 == "loss":
            status_str = "LOSS @stop"
        elif r5 == "win":
            status_str = "WIN 5d"
        elif r5 == "loss":
            status_str = "LOSS 5d"
        elif r1 == "win":
            status_str = "WIN 1d"
        elif r1 == "loss":
            status_str = "LOSS 1d"
        else:
            status_str = "open"
        recent.append({
            "ticker": pick["ticker"],
            "direction": pick["direction"],
            "score": pick.get("score", 0),
            "tier": pick.get("tier", ""),
            "entry": pick["entry"],
            "stop": pick.get("stop", 0),
            "tp1": pick.get("tp1", 0),
            "pnl_pct": pnl_pct,
            "days": days,
            "status": status_str,
            "play_type": pick.get("play_type", "multi_signal"),
        })

    return {
        "has_data": True,
        "stats": stats,
        "recent": recent,
        "total_picks": len(picks),
    }


# ---------------------------------------------------------------------------
# Trade result logs — simple running logs (newest on top)
# ---------------------------------------------------------------------------

def log_trade_results():
    """Append newly resolved trades to winners.txt / losers.txt.
    Each trade is logged once at its earliest resolved window.
    User can delete these files anytime to start fresh."""
    if not PICKS_HISTORY_FILE.exists():
        return 0

    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0

    picks = history.get("picks", [])
    new_entries = {"win": [], "loss": [], "draw": []}
    updated = False

    for pick in picks:
        if pick.get("logged_to_file"):
            continue

        # Find earliest window with a result
        is_day = pick.get("trade_type", "").startswith("DAY TRADE")
        windows = [1] if is_day else [5, 10]
        result = None
        window = None
        exit_price = None

        for w in windows:
            r = pick.get(f"result_{w}d")
            if r is not None:
                result = r
                window = w
                exit_price = pick.get(f"exit_price_{w}d")
                break

        if result is None:
            continue

        # Format the log line
        ticker = pick.get("ticker", "?")
        direction = "LONG" if pick.get("direction") == "bullish" else "SHORT"
        tier = pick.get("tier", "?")
        score = pick.get("score", 0)
        entry = pick.get("entry", 0)
        stop = pick.get("stop", 0)
        tp1 = pick.get("tp1", 0)
        play = PLAY_TYPES.get(pick.get("play_type", ""), pick.get("play_type", "?"))
        regime = pick.get("regime", "?").upper()
        scan_date = pick.get("scan_date", "?")
        signals = pick.get("signal_indicators", [])

        # PnL calculation
        if exit_price and entry:
            if pick.get("direction") == "bullish":
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_price) / entry * 100
        else:
            pnl_pct = 0

        exit_str = f"${exit_price:.2f}" if exit_price else "n/a"
        pnl_sign = "+" if pnl_pct >= 0 else ""
        sig_str = ", ".join(signals[:5]) if signals else "n/a"
        trade_type = "DAY" if is_day else "SWING"

        tp1_tag = " [TP1 hit→WIN]" if pick.get("tp1_upgraded") else ""
        earn_tag = " [EARNINGS GAP - excluded]" if pick.get("earnings_gap") else ""
        line = (f"{scan_date}  {ticker:6s}  {direction:5s}  {tier}-TIER  {score:.1f}  "
                f"${entry:.2f}->{exit_str} {pnl_sign}{pnl_pct:.1f}% ({window}d {trade_type})  "
                f"[{play}]  Regime:{regime}  Signals: {sig_str}{tp1_tag}{earn_tag}")

        new_entries[result].append(line)
        pick["logged_to_file"] = True
        updated = True

    if updated:
        PICKS_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")

    # Prepend new entries to files (newest on top)
    total_logged = 0
    for result_type, entries in new_entries.items():
        if not entries or result_type == "draw":
            continue
        fname = "winners.txt" if result_type == "win" else "losers.txt"
        path = REPORTS_DIR / fname
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
        new_block = "\n".join(entries) + "\n"
        path.write_text(new_block + existing, encoding="utf-8")
        total_logged += len(entries)

    return total_logged


# ---------------------------------------------------------------------------
# Losers post-mortem analysis
# ---------------------------------------------------------------------------

def _classify_loss(pick: dict, window: int) -> dict:
    """Determine how a pick lost — gap through stop, near miss, drift, or immediate reversal."""
    entry = pick.get("entry", 0)
    stop = pick.get("stop", 0)
    tp1 = pick.get("tp1", 0)
    is_long = pick.get("direction") == "bullish"
    exit_price = pick.get(f"exit_price_{window}d")
    high = pick.get("high_since", entry)
    low = pick.get("low_since", entry)

    # How far toward TP1 did it get? (0% = never moved, 100% = hit TP1)
    if is_long:
        tp_range = tp1 - entry if tp1 != entry else 1
        best_progress = (high - entry) / tp_range * 100
        gapped = exit_price is not None and stop > 0 and exit_price < stop * 0.995
    else:
        tp_range = entry - tp1 if tp1 != entry else 1
        best_progress = (entry - low) / tp_range * 100
        gapped = exit_price is not None and stop > 0 and exit_price > stop * 1.005

    best_progress = max(0, min(best_progress, 100))

    if gapped:
        mode = "GAP_THROUGH_STOP"
        desc = "Gapped through stop"
    elif best_progress >= 75:
        mode = "NEAR_MISS"
        desc = f"Reached {best_progress:.0f}% of TP1, then reversed"
    elif best_progress < 25:
        mode = "IMMEDIATE_REVERSAL"
        desc = "Never moved toward target"
    else:
        mode = "DRIFT_TO_STOP"
        desc = "Drifted against position to stop"

    # Causal analysis — WHY did this trade fail?
    causes = []
    trade_type = pick.get("trade_type", "")
    setup_ctx = pick.get("setup_context", "with_trend")
    sector_st = pick.get("sector_status", "")
    vol = pick.get("vol_ratio")
    regime_str = pick.get("regime", "unknown")

    if "market disagrees" in trade_type:
        causes.append("Traded against market trend (SPY/QQQ)")
    if "against 200 MA" in trade_type or "counter-trend" in trade_type:
        causes.append("Counter-trend setup against 200 MA")
    if is_long and sector_st == "lagging":
        causes.append("Longed in a LAGGING sector")
    elif not is_long and sector_st == "leading":
        causes.append("Shorted in a LEADING sector")
    if vol is not None and vol < 1.0:
        causes.append(f"Weak volume ({vol:.1f}x) — no conviction")
    if regime_str == "chop":
        causes.append("Chop regime — no clear trend")
    if setup_ctx == "counter_trend" and "Counter-trend" not in str(causes):
        causes.append("Reversal setup (counter-trend)")

    return {
        "mode": mode,
        "desc": desc,
        "near_miss": best_progress >= 75,
        "tp1_progress_pct": round(best_progress, 1),
        "gapped": gapped,
        "causes": causes,
    }


def analyze_losers() -> dict:
    """Analyze all losing picks to find patterns and failure modes."""
    if not PICKS_HISTORY_FILE.exists():
        return {"has_data": False}

    try:
        history = json.loads(PICKS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"has_data": False}

    all_picks = history.get("picks", []) if isinstance(history, dict) else history

    losers = []
    winners = []

    for pick in all_picks:
        # Skip C-tier — watchlist picks, too noisy for post-mortem
        if pick.get("tier") == "C":
            continue
        # Skip earnings gap — binary event, not a setup failure
        if pick.get("earnings_gap"):
            continue
        is_day = pick.get("trade_type", "").startswith("DAY TRADE")
        # Check each window for results
        for d in TRACK_DAYS:
            if d == 1 and not is_day:
                continue
            if d > 1 and is_day:
                continue
            result = pick.get(f"result_{d}d")
            if result == "loss":
                losers.append({
                    "ticker": pick.get("ticker", "?"),
                    "direction": pick.get("direction", "bullish"),
                    "play_type": pick.get("play_type", "multi_signal"),
                    "play_tags": pick.get("play_tags", []),
                    "signal_indicators": pick.get("signal_indicators", []),
                    "entry": pick.get("entry", 0),
                    "stop": pick.get("stop", 0),
                    "tp1": pick.get("tp1", 0),
                    "exit_price": pick.get(f"exit_price_{d}d"),
                    "high_since": pick.get("high_since"),
                    "low_since": pick.get("low_since"),
                    "window": f"{d}d",
                    "score": pick.get("score", 0),
                    "tier": pick.get("tier", "?"),
                    "regime": pick.get("regime", "unknown"),
                    "sector": pick.get("sector", "Unknown"),
                    "sector_status": pick.get("sector_status", "unknown"),
                    "vol_ratio": pick.get("vol_ratio", 0),
                    "rs_raw": pick.get("rs_raw"),
                    "scan_date": pick.get("scan_date", ""),
                    "failure": _classify_loss(pick, d),
                })
            elif result == "win":
                winners.append({
                    "signal_indicators": pick.get("signal_indicators", []),
                    "regime": pick.get("regime", "unknown"),
                    "sector_status": pick.get("sector_status", "unknown"),
                })

    if not losers:
        return {"has_data": False}

    total_scored = len(losers) + len(winners)

    # Count losses per window
    by_window = Counter(l["window"] for l in losers)

    # Near misses
    near_misses = [l for l in losers if l["failure"]["near_miss"]]

    # Pattern aggregation
    patterns = {
        "by_signal": Counter(),
        "by_play_type": Counter(),
        "by_regime": Counter(),
        "by_sector_status": Counter(),
        "by_tier": Counter(),
        "by_failure_mode": Counter(),
        "by_cause": Counter(),
    }
    for l in losers:
        for sig in l["signal_indicators"]:
            patterns["by_signal"][sig] += 1
        patterns["by_play_type"][l["play_type"]] += 1
        patterns["by_regime"][l["regime"]] += 1
        patterns["by_sector_status"][l["sector_status"]] += 1
        patterns["by_tier"][l["tier"]] += 1
        patterns["by_failure_mode"][l["failure"]["mode"]] += 1
        for cause in l["failure"].get("causes", []):
            patterns["by_cause"][cause] += 1

    # Signal toxicity — compare signal freq on losers vs winners
    winner_signals = Counter()
    for w in winners:
        for sig in w["signal_indicators"]:
            winner_signals[sig] += 1

    signal_toxicity = []
    all_signals = set(patterns["by_signal"].keys()) | set(winner_signals.keys())
    for sig in all_signals:
        on_losers = patterns["by_signal"].get(sig, 0)
        on_winners = winner_signals.get(sig, 0)
        total = on_losers + on_winners
        if total >= 2:
            rate = on_losers / total
            signal_toxicity.append({
                "signal": sig,
                "on_losers": on_losers,
                "on_winners": on_winners,
                "loser_rate": round(rate * 100, 1),
                "total": total,
            })
    signal_toxicity.sort(key=lambda x: x["loser_rate"], reverse=True)

    # Regime loss rates
    regime_loss_rates = {}
    for regime_val in set(l["regime"] for l in losers) | set(w["regime"] for w in winners):
        w_count = sum(1 for w in winners if w["regime"] == regime_val)
        l_count = sum(1 for l in losers if l["regime"] == regime_val)
        total = w_count + l_count
        regime_loss_rates[regime_val] = {
            "wins": w_count, "losses": l_count,
            "rate": round(l_count / total * 100, 1) if total > 0 else 0,
        }

    # Sector status loss rates
    sector_loss_rates = {}
    for ss in set(l["sector_status"] for l in losers) | set(w["sector_status"] for w in winners):
        w_count = sum(1 for w in winners if w["sector_status"] == ss)
        l_count = sum(1 for l in losers if l["sector_status"] == ss)
        total = w_count + l_count
        sector_loss_rates[ss] = {
            "wins": w_count, "losses": l_count,
            "rate": round(l_count / total * 100, 1) if total > 0 else 0,
        }

    return {
        "has_data": True,
        "total_losses": len(losers),
        "total_wins": len(winners),
        "total_scored": total_scored,
        "by_window": dict(by_window),
        "losers": losers,
        "near_misses": near_misses,
        "patterns": patterns,
        "signal_toxicity": signal_toxicity,
        "regime_loss_rates": regime_loss_rates,
        "sector_loss_rates": sector_loss_rates,
        "sample_warning": total_scored < 30,
    }


# ---------------------------------------------------------------------------
# Missed mover history — persistent storage + pattern gap analysis
# ---------------------------------------------------------------------------

def save_missed_movers(movers: list, regime: str):
    """Append this scan's missed movers (with fingerprints) to persistent history.
    Keeps last 50 scans. Each mover's fingerprint captures technical conditions
    that existed when the stock made a big move without being flagged."""
    history = []
    if MISSED_MOVERS_FILE.exists():
        try:
            history = json.loads(MISSED_MOVERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []

    scan_entry = {
        "scan_date": date.today().isoformat(),
        "scan_timestamp": datetime.now().isoformat(),
        "regime": regime,
        "movers": movers,
    }
    history.append(scan_entry)
    history = history[-50:]  # rolling window

    MISSED_MOVERS_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def load_missed_movers_history() -> list:
    """Load all missed movers from history, flattened into a single list.
    Each mover gets scan_date and regime attached."""
    if not MISSED_MOVERS_FILE.exists():
        return []
    try:
        history = json.loads(MISSED_MOVERS_FILE.read_text(encoding="utf-8"))
        all_movers = []
        for entry in history:
            for m in entry.get("movers", []):
                m["scan_date"] = entry.get("scan_date", "")
                m["regime"] = entry.get("regime", "unknown")
                all_movers.append(m)
        return all_movers
    except Exception:
        return []


def _dedup_movers(movers: list) -> list:
    """Deduplicate movers so the same ticker only counts once per direction.
    When a stock drops 18% over 3 days it shows up on consecutive scans —
    without dedup it inflates the pattern statistics. Keep the entry with
    the largest absolute 3d move for each (ticker, direction) pair."""
    best = {}  # (ticker, direction) -> mover
    for m in movers:
        direction = "up" if m.get("move_3d", 0) > 0 else "dn"
        key = (m.get("ticker", ""), direction)
        existing = best.get(key)
        if existing is None or abs(m.get("move_3d", 0)) > abs(existing.get("move_3d", 0)):
            best[key] = m
    return list(best.values())


def analyze_pattern_gaps() -> dict:
    """Analyze accumulated missed movers to find recurring technical patterns
    not covered by existing signals.

    For each boolean feature in the fingerprint, counts how often it appears
    in missed movers and compares to whether the corresponding signal fired.
    High-frequency features with low signal fire rates = signal gaps.

    Filters applied before analysis:
    - Catalyst events excluded (gap + extreme volume = earnings/news)
    - Signal conflicts excluded (detected but contradicting — not a gap)
    - Same ticker deduplicated across scans (keeps largest move)

    Returns dict with up/down gap lists for the report."""
    all_movers = load_missed_movers_history()
    # Only movers that have fingerprints
    all_movers = [m for m in all_movers if m.get("fingerprint")]

    # Exclude catalyst events — gap + volume spike = not a technical miss
    all_movers = [
        m for m in all_movers
        if not m.get("fingerprint", {}).get("likely_catalyst")
    ]

    # Exclude signal conflicts — system detected but signals contradicted.
    # These aren't coverage gaps, they're conflict-resolution issues.
    all_movers = [
        m for m in all_movers
        if m.get("miss_type", "true_miss") != "conflict"
    ]

    # Exclude earnings-driven movers — move caused by earnings event, not a signal gap
    all_movers = [
        m for m in all_movers
        if not m.get("earnings_driven", False)
        and "Post-Earnings" not in m.get("signals_fired", [])
        and (m.get("earn_days_since") is None or m["earn_days_since"] > 14)
    ]

    # Deduplicate: same ticker appearing on consecutive scans for the same
    # multi-day move inflates the statistics
    all_movers = _dedup_movers(all_movers)

    if len(all_movers) < 10:
        return {"has_data": False}

    up_movers = [m for m in all_movers if m.get("move_3d", 0) > 0]
    dn_movers = [m for m in all_movers if m.get("move_3d", 0) < 0]

    # Human-readable feature labels
    feature_labels = {
        "obv_diverging_up": "OBV Divergence (volume in, price flat)",
        "obv_diverging_down": "OBV Divergence (volume out, price flat)",
        "accumulation_vol": "Accumulation Volume (buy/sell > 1.3x)",
        "distribution_vol": "Distribution Volume (buy/sell < 0.77x)",
        "tight_base": "Tight Base (20d range < 10%)",
        "higher_lows": "Higher Lows forming",
        "lower_highs": "Lower Highs forming",
        "vol_expanding": "Volume Expanding (3d > 10d avg)",
        "near_support": "Near Support Level",
        "near_resistance": "Near Resistance Level",
        "gap_recent": "Recent Gap (2%+)",
        "rsi_near_oversold": "RSI Near Oversold (30-40)",
        "rsi_near_overbought": "RSI Near Overbought (60-70)",
    }

    # Feature -> which existing signals should cover this condition
    # Empty list = genuine gap (no signal exists for this)
    signal_coverage = {
        "obv_diverging_up": ["Accumulation"],
        "obv_diverging_down": ["Accumulation"],
        "accumulation_vol": ["Accumulation", "Early Accumulation"],
        "distribution_vol": ["Accumulation", "Early Accumulation"],
        "tight_base": ["VCP Breakout", "Range Compression"],
        "higher_lows": ["Higher Lows"],
        "lower_highs": ["Dist Breakdown"],
        "vol_expanding": ["Trend Accel", "Volume Climax"],
        "near_support": ["Support/Resist"],
        "near_resistance": ["Support/Resist"],
        "gap_recent": ["Gap Fill", "Post-Earnings"],
        "rsi_near_oversold": ["RSI"],
        "rsi_near_overbought": ["RSI"],
    }

    # Features to check per direction
    bullish_features = [
        "obv_diverging_up", "accumulation_vol", "tight_base",
        "higher_lows", "vol_expanding", "near_support",
        "gap_recent", "rsi_near_oversold",
    ]
    bearish_features = [
        "obv_diverging_down", "distribution_vol", "tight_base",
        "lower_highs", "vol_expanding", "near_resistance",
        "gap_recent", "rsi_near_overbought",
    ]

    def _analyze_direction(movers, features):
        gaps = []
        if len(movers) < 5:
            return gaps
        for feat in features:
            fps_with_feat = [
                m for m in movers if m.get("fingerprint", {}).get(feat)
            ]
            count = len(fps_with_feat)
            freq = count / len(movers)
            if freq < 0.30:  # only report features in 30%+ of misses
                continue

            # How often did the covering signal actually fire?
            coverage_sigs = signal_coverage.get(feat, [])
            if coverage_sigs:
                fired_count = sum(
                    1 for m in fps_with_feat
                    if any(
                        s in m.get("fingerprint", {}).get("signals_fired", [])
                        for s in coverage_sigs
                    )
                )
                fired_rate = fired_count / count if count > 0 else 0
            else:
                fired_count = 0
                fired_rate = 0

            gaps.append({
                "feature": feat,
                "label": feature_labels.get(feat, feat),
                "frequency": round(freq * 100),
                "count": count,
                "total": len(movers),
                "coverage_signals": coverage_sigs,
                "signal_fire_rate": round(fired_rate * 100),
            })
        return sorted(gaps, key=lambda x: x["frequency"], reverse=True)

    up_gaps = _analyze_direction(up_movers, bullish_features)
    dn_gaps = _analyze_direction(dn_movers, bearish_features)

    # Scan count (how many unique scan dates)
    scan_dates = {m.get("scan_date", "") for m in all_movers}

    return {
        "has_data": True,
        "total_movers": len(all_movers),
        "scan_count": len(scan_dates),
        "up_count": len(up_movers),
        "dn_count": len(dn_movers),
        "up_gaps": up_gaps,
        "dn_gaps": dn_gaps,
    }
