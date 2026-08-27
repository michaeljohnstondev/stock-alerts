import json
import math
from datetime import date

from .constants import (
    USER_PREFS_FILE, FAVORITES_FILE, ALERTS_FILE, PLAY_TRACKER_FILE,
    PREF_MAX_BOOST, PREF_MAX_PENALTY, PREF_HALF_LIFE_DAYS,
    PREF_DIMENSION_WEIGHTS,
)


def load_user_preferences() -> dict:
    """Load user preferences from disk. Returns empty structure if none exist."""
    if USER_PREFS_FILE.exists():
        try:
            return json.loads(USER_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"selections": [], "profile": {}}


def _save_user_preferences(prefs: dict):
    """Persist user preferences to disk."""
    USER_PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def record_favorites(results: list, selected_tickers: list):
    """Record user's favorite picks after a scan.
    selected_tickers: list of ticker strings the user chose."""
    prefs = load_user_preferences()
    today = date.today().isoformat()

    selected_set = set(selected_tickers)
    for r in results:
        if r["ticker"] not in selected_set:
            continue
        prefs["selections"].append({
            "ticker": r["ticker"],
            "play_type": r.get("play_type", "multi_signal"),
            "play_tags": r.get("play_tags", []),
            "signals": [s["indicator"] for s in r.get("signals", [])],
            "setup_context": r.get("setup_context", "with_trend"),
            "direction": r.get("direction", "bullish"),
            "sector_status": r.get("sector_status"),
            "date": today,
        })

    # Rolling window — keep last 200 selections
    prefs["selections"] = prefs["selections"][-200:]

    _recompute_profile(prefs)
    _save_user_preferences(prefs)


def _recompute_profile(prefs: dict):
    """Rebuild the preference profile from selections with time decay."""
    selections = prefs.get("selections", [])
    if not selections:
        prefs["profile"] = {}
        return

    today = date.today()

    # Accumulate weighted counts per dimension
    play_type_counts = {}
    signal_counts = {}
    context_counts = {}
    direction_counts = {}
    sector_counts = {}

    for sel in selections:
        # Time decay: weight = 0.5 ^ (days_since / half_life)
        try:
            sel_date = date.fromisoformat(sel["date"])
            days_since = (today - sel_date).days
        except Exception:
            days_since = 90  # old/invalid date gets low weight

        weight = math.pow(0.5, days_since / PREF_HALF_LIFE_DAYS)

        # Play type
        pt = sel.get("play_type", "multi_signal")
        play_type_counts[pt] = play_type_counts.get(pt, 0) + weight

        # Signals — each signal in the pick gets credit
        for sig in sel.get("signals", []):
            signal_counts[sig] = signal_counts.get(sig, 0) + weight

        # Setup context
        ctx = sel.get("setup_context", "with_trend")
        context_counts[ctx] = context_counts.get(ctx, 0) + weight

        # Direction
        d = sel.get("direction", "bullish")
        direction_counts[d] = direction_counts.get(d, 0) + weight

        # Sector status
        ss = sel.get("sector_status")
        if ss:
            sector_counts[ss] = sector_counts.get(ss, 0) + weight

    # Normalize each dimension to [0, 1] — max value gets 1.0
    def _normalize(counts: dict) -> dict:
        if not counts:
            return {}
        max_val = max(counts.values())
        if max_val <= 0:
            return {}
        return {k: round(v / max_val, 3) for k, v in counts.items()}

    prefs["profile"] = {
        "play_type": _normalize(play_type_counts),
        "signals": _normalize(signal_counts),
        "setup_context": _normalize(context_counts),
        "direction": _normalize(direction_counts),
        "sector_status": _normalize(sector_counts),
        "total_selections": len(selections),
        "last_updated": today.isoformat(),
    }


def compute_preference_boost(result: dict, prefs: dict) -> float:
    """Compute win rate boost/penalty based on how well a pick matches user preferences.
    Returns a float in [-PREF_MAX_BOOST, +PREF_MAX_BOOST] (e.g. -0.05 to +0.05)."""
    profile = prefs.get("profile", {})
    if not profile or profile.get("total_selections", 0) < 5:
        return 0.0

    scores = {}

    # Play type match
    pt_profile = profile.get("play_type", {})
    scores["play_type"] = pt_profile.get(result.get("play_type", ""), 0.0)

    # Signal match — average profile score of all signals in the result
    sig_profile = profile.get("signals", {})
    result_signals = [s["indicator"] for s in result.get("signals", [])]
    if result_signals and sig_profile:
        sig_scores = [sig_profile.get(s, 0.0) for s in result_signals]
        scores["signals"] = sum(sig_scores) / len(sig_scores)
    else:
        scores["signals"] = 0.0

    # Setup context match
    ctx_profile = profile.get("setup_context", {})
    scores["setup_context"] = ctx_profile.get(result.get("setup_context", ""), 0.0)

    # Direction match
    dir_profile = profile.get("direction", {})
    scores["direction"] = dir_profile.get(result.get("direction", ""), 0.0)

    # Sector status match (None → neutral 0.5)
    sec_profile = profile.get("sector_status", {})
    sec_status = result.get("sector_status")
    scores["sector_status"] = sec_profile.get(sec_status, 0.5) if sec_status else 0.5

    # Weighted sum across dimensions
    raw = sum(
        PREF_DIMENSION_WEIGHTS.get(dim, 0) * scores.get(dim, 0)
        for dim in PREF_DIMENSION_WEIGHTS
    )

    # Center at 0 (average pick gets ~0 boost) and scale to max range
    centered = raw - 0.5
    boost = centered * 2 * PREF_MAX_BOOST

    return max(-PREF_MAX_PENALTY, min(PREF_MAX_BOOST, boost))


def record_favorite_setups(setup_dicts: list):
    """Record specific setups as favorites (from deep dive).
    Unlike record_favorites(), takes pre-built setup dicts directly."""
    prefs = load_user_preferences()
    today = date.today().isoformat()
    for s in setup_dicts:
        prefs["selections"].append({
            "ticker": s["ticker"],
            "play_type": s.get("play_type", "multi_signal"),
            "play_tags": s.get("play_tags", []),
            "signals": [sig["indicator"] for sig in s.get("signals", [])],
            "setup_context": s.get("setup_context", "with_trend"),
            "direction": s.get("direction", "bullish"),
            "sector_status": s.get("sector_status"),
            "date": today,
        })
    prefs["selections"] = prefs["selections"][-200:]
    _recompute_profile(prefs)
    _save_user_preferences(prefs)


# ---------------------------------------------------------------------------
# Favorites — tickers that always show in the report
# ---------------------------------------------------------------------------

def load_favorites() -> set:
    """Load favorite tickers from disk. Returns set of uppercase ticker strings."""
    if FAVORITES_FILE.exists():
        try:
            data = json.loads(FAVORITES_FILE.read_text(encoding="utf-8"))
            return {t.upper() for t in data if isinstance(t, str)}
        except Exception:
            pass
    return set()


def _save_favorites(favs: set):
    FAVORITES_FILE.write_text(json.dumps(sorted(favs), indent=2), encoding="utf-8")


def add_favorites(tickers: list) -> list:
    """Add tickers to favorites. Returns list of newly added tickers."""
    favs = load_favorites()
    added = []
    for t in tickers:
        t = t.upper().strip()
        if t and t not in favs:
            favs.add(t)
            added.append(t)
    _save_favorites(favs)
    return added


def remove_favorites(tickers: list) -> list:
    """Remove tickers from favorites. Returns list of actually removed tickers."""
    favs = load_favorites()
    removed = []
    for t in tickers:
        t = t.upper().strip()
        if t in favs:
            favs.discard(t)
            removed.append(t)
    _save_favorites(favs)
    return removed


# ---------------------------------------------------------------------------
# Price Alerts — target prices that trigger in the report
# ---------------------------------------------------------------------------

def load_alerts() -> dict:
    """Load price alerts from disk. Returns dict {TICKER: {price, direction}}."""
    if ALERTS_FILE.exists():
        try:
            return json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_alerts(alerts: dict):
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2), encoding="utf-8")


def add_alert(ticker: str, price: float, direction: str = "below") -> bool:
    """Set a price alert. direction is 'above' or 'below'. Returns True if added/updated."""
    alerts = load_alerts()
    ticker = ticker.upper().strip()
    direction = direction.lower().strip()
    if not ticker or price <= 0 or direction not in ("above", "below"):
        return False
    alerts[ticker] = {"price": round(price, 2), "direction": direction}
    _save_alerts(alerts)
    return True


def remove_alert(ticker: str) -> bool:
    """Remove a price alert. Returns True if it existed."""
    alerts = load_alerts()
    ticker = ticker.upper().strip()
    if ticker in alerts:
        del alerts[ticker]
        _save_alerts(alerts)
        return True
    return False


# ---------------------------------------------------------------------------
# Play Tracker — win/loss tracking per play type
# ---------------------------------------------------------------------------

_EVAL_DAYS = 5  # evaluate outcome after 5 trading days


def _load_tracker() -> dict:
    if PLAY_TRACKER_FILE.exists():
        try:
            return json.loads(PLAY_TRACKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"plays": [], "stats": {}}


def _save_tracker(data: dict):
    PLAY_TRACKER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_plays(leaderboard_items: dict, earnings_map: dict = None):
    """Record today's plays from signal leaderboards.
    leaderboard_items: dict keyed by (signal, direction) -> list of items
    earnings_map: dict {ticker: earnings_dict} to check proximity
    """
    tracker = _load_tracker()
    today = date.today().isoformat()
    earnings_map = earnings_map or {}

    # Featured play signals we track
    _tracked = {
        "M on Uptrend", "M on Downtrend", "M Breakout Up", "M Breakout Down",
        "Support/Resist", "Double Bottom", "5 EMA Touch", "200 MA Touch",
        "EMA21 Pullback", "RSI",
        "Earnings Beat Drift", "Earnings Overreaction", "Earnings Anticipation",
    }

    # Already recorded today — avoid duplicates
    _today_recorded = {
        (p["ticker"], p["signal"]) for p in tracker["plays"]
        if p["date"] == today
    }

    for key, items in leaderboard_items.items():
        signal_name, direction = key
        if signal_name not in _tracked or direction != "bullish":
            continue
        for item in items[:15]:  # only top 15 per play
            tkr = item.get("ticker", "")
            if (tkr, signal_name) in _today_recorded:
                continue

            # Check earnings proximity — skip non-earnings plays if within 10 days
            # (earnings-based plays ARE the earnings event, so don't skip them)
            _earnings_plays = {"Earnings Beat Drift", "Earnings Overreaction", "Earnings Anticipation"}
            if signal_name not in _earnings_plays:
                earn = earnings_map.get(tkr) or item.get("earnings")
                if earn:
                    du = earn.get("days_until")
                    ds = earn.get("days_since")
                    if (du is not None and du <= 10) or (ds is not None and ds <= 10):
                        continue

            tracker["plays"].append({
                "ticker": tkr,
                "signal": signal_name,
                "entry_price": item.get("price", 0),
                "date": today,
                "outcome": None,
            })

    # Keep last 500 plays
    tracker["plays"] = tracker["plays"][-500:]
    _save_tracker(tracker)


def evaluate_plays(current_prices: dict):
    """Evaluate pending plays every run. Win = +1% from entry, Loss = -1%."""
    tracker = _load_tracker()
    today = date.today()
    changed = False

    for play in tracker["plays"]:
        if play["outcome"] is not None:
            continue

        tkr = play["ticker"]
        cur = current_prices.get(tkr)
        if cur is None or cur <= 0:
            continue

        entry = play["entry_price"]
        if entry <= 0:
            continue

        pct = (cur - entry) / entry

        if pct >= 0.01:
            play["outcome"] = "win"
        elif pct <= -0.01:
            play["outcome"] = "loss"
        else:
            continue  # still within ±1%, check again next run

        try:
            play_date = date.fromisoformat(play["date"])
            play["days_held"] = (today - play_date).days
        except Exception:
            play["days_held"] = 0
        play["exit_price"] = round(cur, 2)
        play["pct"] = round(pct * 100, 2)
        changed = True

    if changed:
        _recompute_play_stats(tracker)
        _save_tracker(tracker)


def _recompute_play_stats(tracker: dict):
    """Rebuild stats from evaluated plays."""
    stats = {}
    for play in tracker["plays"]:
        sig = play["signal"]
        if sig not in stats:
            stats[sig] = {"wins": 0, "losses": 0, "pending": 0}
        if play["outcome"] == "win":
            stats[sig]["wins"] += 1
        elif play["outcome"] == "loss":
            stats[sig]["losses"] += 1
        else:
            stats[sig]["pending"] += 1
    tracker["stats"] = stats


def get_play_stats() -> dict:
    """Return play win rates. {signal_name: {wins, losses, pending, win_pct, total}}"""
    tracker = _load_tracker()
    result = {}
    for sig, counts in tracker.get("stats", {}).items():
        w = counts.get("wins", 0)
        l = counts.get("losses", 0)
        total = w + l
        result[sig] = {
            "wins": w,
            "losses": l,
            "pending": counts.get("pending", 0),
            "total": total,
            "win_pct": round(w / total * 100) if total > 0 else None,
        }
    return result
