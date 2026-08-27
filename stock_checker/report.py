import json
from collections import Counter
from datetime import datetime

from .constants import (
    PLAY_TYPES,
    REPORTS_DIR, LAST_SCAN_FILE,
    EARNINGS_NOTE_DAYS,
)


# ---------------------------------------------------------------------------
# Scan snapshot — save/load/diff for report-to-report comparison
# ---------------------------------------------------------------------------

def save_scan_snapshot(results: list, regime: str, market_trends: dict):
    """Save a lightweight snapshot of current scan results for next-run comparison."""
    picks = []
    for r in results:
        picks.append({
            "ticker": r["ticker"],
            "direction": r["direction"],
            "score": r["score"],
            "tier": r["tier"],
            "trade_type": r["trade_type"],
            "play_type": r.get("play_type", "multi_signal"),
            "ev_ratio": r.get("ev", {}).get("ev_ratio", 0),
            "win_rate": r.get("ev", {}).get("win_rate", 0),
            "vol_ratio": r.get("vol_ratio", 0),
            "rs_raw": r.get("rs_raw"),
            "sector": r.get("sector", "Unknown"),
        })
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "regime": regime,
        "market_trends": market_trends,
        "picks": picks,
    }
    LAST_SCAN_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")



# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------


def write_report(results: list, market_trends: dict, config: dict,
                  regime: str = "unknown", regime_change: dict = None,
                  developing: list = None, sector_rotation: dict = None,
                  sector_changes: dict = None,
                  market_context: dict = None,
                  rotation_divergence: dict = None, dip_watch: list = None,
                  favorites: set = None,
                  signal_leaderboards: dict = None,
                  alert_status: list = None,
                  play_stats: dict = None,
                  all_leaderboard: list = None):
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = REPORTS_DIR / f"report_{timestamp}.txt"
    if regime_change is None:
        regime_change = {"changed": False}
    if developing is None:
        developing = []
    if sector_rotation is None:
        sector_rotation = {}
    if sector_changes is None:
        sector_changes = {"has_prev": False, "changes": {}, "transitions": []}

    # Sort by score first, then EV ratio as tiebreaker
    results.sort(key=lambda r: (r["score"], r["ev"]["ev_ratio"]), reverse=True)

    # Sector diversification: flag sectors with 3+ trades
    sector_counts = Counter(r["sector"] for r in results)
    crowded_sectors = {s for s, c in sector_counts.items() if c >= 4}

    L = []  # lines
    L.append("")
    L.append("  ================================================================")
    L.append("    DAILY STOCK REPORT")
    L.append(f"    {datetime.now().strftime('%A, %B %d %Y  %I:%M %p')}")
    L.append("  ================================================================")
    L.append("")
    regime_labels = {
        "chop": "CHOP (structure only, scores capped at 8.0)",
        "range": "RANGE (fade extremes)",
        "trend": "TREND (continuation setups)",
        "unknown": "UNKNOWN",
    }
    L.append(f"    Market:   {', '.join(f'{k} = {v.upper()}' for k, v in market_trends.items())}")
    L.append(f"    Regime:   {regime_labels.get(regime, regime)}")

    # Rotation divergence warning — market and sector brains disagree
    if rotation_divergence and rotation_divergence.get("divergent"):
        L.append(f"    !! ROTATION DIVERGENCE: {rotation_divergence['reason']}")

    # Market context (rates, gold, crypto)
    if market_context:
        ctx_parts = []
        for sym, info in market_context.items():
            trend = info["trend"].upper()
            pct = info.get("pct_above_200", 0)
            ctx_parts.append(f"{info['label']} = {trend} ({pct:+.1f}%)")
        L.append(f"    Context:  {', '.join(ctx_parts)}")

    # --- REGIME CHANGE ALERT ---
    if regime_change.get("changed"):
        fr = regime_change["from_regime"].upper()
        to = regime_change["to_regime"].upper()
        fa = regime_change.get("from_adx", "?")
        ta = regime_change.get("to_adx", "?")
        if to == "TREND":
            hint = "new setups emerging"
        elif to == "CHOP":
            hint = "tighten stops, reduce size"
        else:
            hint = "watch for direction"
        L.append(f"    >>> REGIME CHANGE: {fr} -> {to} (ADX {fa} -> {ta}) — {hint}")

    L.append(f"    Scanned:  {len(config['tickers'])} tickers")
    L.append(f"    Winners:  {len(results)}")
    if developing:
        L.append(f"    VCP Watch: {len(developing)} stocks forming volatility contraction")
    if dip_watch:
        picked_tickers_hdr = {r["ticker"] for r in results}
        dip_count = sum(1 for d in dip_watch if d["ticker"] not in picked_tickers_hdr)
        if dip_count > 0:
            L.append(f"    Dip Watch: {dip_count} stocks extended below key MAs")
    if crowded_sectors:
        L.append(f"    Crowded:  {', '.join(crowded_sectors)} (4+ trades — diversify)")
    L.append("")

    # --- SECTOR ROTATION (inline) ---
    if sector_rotation:
        has_prev = bool(sector_changes and sector_changes.get("has_prev"))
        sc = sector_changes.get("changes", {}) if sector_changes else {}
        L.append("  ================================================================")
        L.append("    SECTOR ROTATION (vs SPY)")
        L.append("  ================================================================")
        # Column header aligned with data rows:
        # data = "    #{rank:<3d} {sector:<25s} {chg}{1d}  {10d}  {30d}  comp:{c}"
        # prefix = 4-indent + # + 3-rank + space + 25-sector + space = 4+1+3+1+25+1 = 35
        pad = " " * 31  # 31 chars after the 4-space indent
        if has_prev:
            L.append(f"    {pad} scan     1d     10d     30d     comp")
        else:
            L.append(f"    {pad}   1d     10d     30d     comp")
        L.append("")
        ranked = sorted(sector_rotation.items(), key=lambda x: x[1].get("rank", 99))
        for sector, data in ranked:
            status = data.get("status", "neutral").upper()
            tag = f"  {status}" if status != "NEUTRAL" else ""
            change_str = ""
            chg_col = ""
            if sector in sc:
                rd = sc[sector]["rank_delta"]
                pchg = sc[sector].get("price_chg", 0)
                chg_col = f"{pchg:+5.1f}%  " if has_prev else ""
                if rd > 0:
                    change_str += f"  ^{rd}"
                elif rd < 0:
                    change_str += f"  v{abs(rd)}"
            elif has_prev:
                chg_col = " +0.0%  "
            L.append(f"    #{data['rank']:<3d} {sector:<25s} {chg_col}{data.get('ret_1d', 0):+5.1f}%  {data['ret_10d']:+5.1f}%  {data['ret_30d']:+5.1f}%  comp:{data['composite']:+5.1f}{tag}{change_str}")
        L.append("")
        # Status transitions (compact)
        transitions = sector_changes.get("transitions", []) if sector_changes else []
        if transitions:
            parts = []
            for t in transitions:
                parts.append(f"{t['sector']}: {t['from'].upper()} -> {t['to'].upper()}")
            L.append(f"    Transitions: {' | '.join(parts)}")
            L.append("")
        # Rotation divergence warning
        if rotation_divergence and rotation_divergence.get("divergent"):
            L.append(f"    !! ROTATION DIVERGENCE: {rotation_divergence['reason']}")
            L.append("")

    # Build a lookup of qualified picks for cross-referencing
    _qualified = {}
    for r in results:
        _qualified[r["ticker"]] = r

    # --- FEATURED PLAYS (Top 15 each) ---
    _featured_plays = [
        (("M on Uptrend", "bullish"),   "M ON UPTREND",    "Ascending valleys at trendline support — buy the pullback"),
        (("M on Downtrend", "bullish"), "M ON DOWNTREND",  "Descending valleys projecting V3 — predictive dip entry"),
        (("M Breakout Up", "bullish"),  "M BREAKOUT UP",   "Ascending M broke resistance with volume — momentum entry"),
        (("M Breakout Down", "bullish"),"M BREAKOUT DOWN", "Descending M broke resistance — reversal confirmed"),
        (("Support/Resist", "bullish"), "SUPPORT BOUNCE",  "Stocks bouncing off key support levels"),
        (("Double Bottom", "bullish"),  "DOUBLE BOTTOM",   "Two matching lows with neckline — classic reversal entry"),
        (("5 EMA Touch", "bullish"),    "5 EMA TOUCH",     "Price near the 5 EMA"),
        (("200 MA Touch", "bullish"),   "200 MA TOUCH",    "Price near the 200 MA — key level"),
        (("EMA21 Pullback", "bullish"), "EMA21 PULLBACK",  "Pulled back to EMA21 and bounced — trend continuation"),
        (("RSI", "bullish"),            "RSI OVERSOLD",    "Most extreme oversold RSI readings — potential bounce entries"),
        (("Earnings Beat Drift", "bullish"),   "EARNINGS BEAT DRIFT",   "Beat earnings but stock hasn't caught up — PEAD opportunity"),
        (("Earnings Overreaction", "bullish"), "EARNINGS OVERREACTION", "Missed earnings but stock dropped too much — oversold bounce"),
        (("Earnings Anticipation", "bullish"), "EARNINGS ANTICIPATION", "Oversold stock approaching earnings — pre-earnings run"),
    ]

    # --- Helper: get Val grade (from item value_grade dict, or fallback to _qualified) ---
    def _val_str(tkr, vg=None):
        vd = vg or {}
        if not vd.get("grade") or vd["grade"] == "N/A":
            qual = _qualified.get(tkr)
            if qual:
                vd = qual.get("value_grade", {})
        if vd.get("grade") and vd["grade"] != "N/A":
            return f"Val:{vd['grade']}"
        return ""

    # --- Helper: format earnings tag ---
    def _earn_str(earn, rev=None):
        if not earn and not rev:
            return ""
        parts = []
        du = earn.get("days_until") if earn else None
        ds = earn.get("days_since") if earn else None
        # Show whichever is closer: last or next
        _showing_next = False
        if du is not None and ds is not None:
            if du <= ds:
                parts.append(f"{'EARN +' + str(du) + 'd':10s}")
                _showing_next = True
            else:
                parts.append(f"{'EARN -' + str(ds) + 'd':10s}")
        elif du is not None:
            parts.append(f"{'EARN +' + str(du) + 'd':10s}")
            _showing_next = True
        elif ds is not None:
            parts.append(f"{'EARN -' + str(ds) + 'd':10s}")

        eps = earn.get("eps") if earn else None
        if _showing_next:
            if eps:
                act = eps.get("actual")
                ne = eps.get("next_estimate")
                if act is not None and ne is not None:
                    parts.append(f"Last:${str(act):6s}  Est:${str(ne):6s}")
                elif act is not None:
                    parts.append(f"Last:${str(act):6s}              ")
                else:
                    parts.append(f"{'':28s}")
            else:
                parts.append(f"{'':28s}")
        else:
            if eps:
                surp = eps.get("surprise_pct")
                exp = eps.get("expected_move")
                d = eps.get("drift")
                surp_s = f"EPS:{surp:+.1f}%" if surp is not None else ""
                exp_s = f"Exp:{exp:+.1f}%" if exp is not None else ""
                d_s = f"D:{d:+.1f}" if d is not None else ""
                parts.append(f"{surp_s:11s}  {exp_s:11s}  {d_s:8s}")
            else:
                parts.append(f"{'':35s}")

        # Revenue fair value gap
        if rev:
            gp = rev.get("gap_pct")
            if gp is not None:
                parts.append(f"{'RFV:' + f'{gp:+.1f}%':11s}")
            else:
                parts.append(f"{'':11s}")
        else:
            parts.append(f"{'':11s}")

        return "  |  ".join(parts)

    # --- Helper: format a featured play line ---
    def _fmt_featured_line(rank, tkr, price, key_col, rsi, rs, val, earn=""):
        base = f"    #{rank:<2d}  {tkr:6s}  ${price:<9.2f}  {key_col:24s}  RSI:{rsi:<5s}  {rs:9s}  {val}"
        if earn:
            base += f"  [{earn}]"
        return base

    # --- Helper: get key column per play type ---
    def _key_col_for(play_key, item):
        meta = item.get("meta", {})
        if play_key == ("M on Uptrend", "bullish"):
            sup = meta.get("projected_support")
            return f"Support: ${sup:.2f}" if sup else ""
        elif play_key == ("Support/Resist", "bullish"):
            lvl = meta.get("level")
            return f"Support: ${lvl:.2f}" if lvl else ""
        elif play_key == ("Double Bottom", "bullish"):
            l1 = meta.get("low1")
            l2 = meta.get("low2")
            if l1 and l2:
                return f"Lows: ${l1:.2f} / ${l2:.2f}"
            return ""
        elif play_key == ("M on Downtrend", "bullish"):
            v3 = meta.get("projected_v3")
            return f"V3: ${v3:.2f}" if v3 else ""
        elif play_key in (("M Breakout Up", "bullish"), ("M Breakout Down", "bullish")):
            p2 = meta.get("p2")
            vr = meta.get("vol_ratio")
            if p2 and vr:
                return f"P2: ${p2:.2f} Vol:{vr:.1f}x"
            return ""
        elif play_key == ("5 EMA Touch", "bullish"):
            val = meta.get("ema_val")
            return f"EMA 5: ${val:.2f}" if val else ""
        elif play_key == ("200 MA Touch", "bullish"):
            val = meta.get("ema_val")
            return f"200 MA: ${val:.2f}" if val else ""
        elif play_key == ("EMA21 Pullback", "bullish"):
            val = meta.get("ema_val")
            return f"EMA 21: ${val:.2f}" if val else ""
        elif play_key == ("200 MA", "bullish"):
            val = meta.get("ma_val")
            return f"200 MA: ${val:.2f}" if val else ""
        elif play_key == ("RSI", "bullish"):
            rv = meta.get("value")
            return f"RSI: {rv:.1f}" if rv else ""
        elif play_key in (("Earnings Beat Drift", "bullish"), ("Earnings Overreaction", "bullish")):
            act = meta.get("eps_actual")
            est = meta.get("eps_estimate")
            surp = meta.get("surprise_pct")
            d = meta.get("drift")
            pchg = meta.get("price_chg_pct", 0)
            ds = meta.get("days_since", 0)
            tag = "Beat" if surp and surp > 0 else "Miss"
            exp = meta.get("expected_move")
            parts = []
            if act is not None and est is not None:
                parts.append(f"EPS:{act} vs {est} ({tag} {surp:+.1f}%)")
            parts.append(f"Stock:{pchg:+.1f}%")
            if exp is not None:
                parts.append(f"Exp:{exp:+.1f}%")
            if d is not None:
                parts.append(f"D:{d:+.1f}")
            if ds:
                parts.append(f"-{ds}d")
            return "  ".join(parts)
        elif play_key == ("Earnings Anticipation", "bullish"):
            du = meta.get("days_until")
            rsi_v = meta.get("rsi")
            pb = meta.get("prior_beat")
            parts = []
            if du is not None:
                parts.append(f"EARN +{du}d")
            if rsi_v is not None:
                parts.append(f"RSI:{rsi_v:.0f}")
            if pb:
                parts.append("Prior Beat")
            return "  ".join(parts)
        return ""

    # --- PRICE ALERTS ---
    if alert_status:
        triggered = [a for a in alert_status if a["triggered"]]
        not_triggered = [a for a in alert_status if not a["triggered"]]
        _al_title = "*** PRICE ALERTS TRIGGERED ***" if triggered else "PRICE ALERTS"
        L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
        L.append(f"  ║    {_al_title}")
        L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
        if triggered:
            L.append(f"    {len(triggered)} of {len(alert_status)} alerts triggered:")
            L.append("")
            for a in triggered:
                d = a["direction"]
                pct = abs(a["pct"])
                if d == "below":
                    tag = f"BUY  {pct:.1f}% below target" if a["pct"] < 0 else "BUY  at target"
                else:
                    tag = f"SELL  {pct:.1f}% above target" if a["pct"] > 0 else "SELL  at target"
                L.append(f"    {a['ticker']:6s}  ${a['price']:<9.2f}  target ${a['target']:<9.2f}  {tag}")
            L.append("")
        if not_triggered:
            if triggered:
                L.append("    Not triggered:")
            for a in not_triggered:
                d = a["direction"]
                pct = abs(a["pct"])
                if d == "below":
                    L.append(f"    {a['ticker']:6s}  ${a['price']:<9.2f}  buy below ${a['target']:<9.2f}  ({pct:.1f}% away)")
                else:
                    L.append(f"    {a['ticker']:6s}  ${a['price']:<9.2f}  sell above ${a['target']:<9.2f}  ({pct:.1f}% away)")
            L.append("")

    # --- CONFLUENCE — Top 25 stocks appearing in 2+ featured plays ---
    # Scans ALL qualifying stocks per play (not just top 15) for maximum overlap
    _confluence_map = {}
    for _fp_key, _fp_title, _ in _featured_plays:
        for item in (signal_leaderboards or {}).get(_fp_key, []):
            tkr = item.get("ticker", "???")
            if tkr not in _confluence_map:
                _confluence_map[tkr] = {
                    "plays": [],
                    "price": item.get("price", 0),
                    "rs_raw": item.get("rs_raw"),
                    "rsi": item.get("rsi"),
                    "value_grade": item.get("value_grade"),
                    "earnings": item.get("earnings"),
                    "revenue": item.get("revenue"),
                }
            _confluence_map[tkr]["plays"].append(_fp_title)

    _confluence_list = [
        (tkr, info) for tkr, info in _confluence_map.items()
        if len(info["plays"]) >= 2
    ]
    # Sort: most signals first, then cheapest by revenue fair value (most undervalued)
    def _cf_sort_key(x):
        n_plays = -len(x[1]["plays"])
        rev = x[1].get("revenue") or {}
        gap_pct = rev.get("gap_pct", 999)  # lower = cheaper, 999 = no data (sort last)
        return (n_plays, gap_pct)
    _confluence_list.sort(key=_cf_sort_key)

    # Short codes for confluence display
    _play_short = {
        "M ON UPTREND": "MU",
        "M ON DOWNTREND": "MD",
        "M BREAKOUT UP": "BU",
        "M BREAKOUT DOWN": "BD",
        "SUPPORT BOUNCE": "SUP",
        "DOUBLE BOTTOM": "DB",
        "5 EMA TOUCH": "5E",
        "200 MA TOUCH": "200",
        "EMA21 PULLBACK": "E21",
        "RSI OVERSOLD": "RSI",
        "EARNINGS BEAT DRIFT": "EBD",
        "EARNINGS OVERREACTION": "EOR",
        "EARNINGS ANTICIPATION": "EA",
    }

    if _confluence_list:
        _cf_n = min(len(_confluence_list), 25)
        L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
        L.append(f"  ║    CONFLUENCE — Top {_cf_n} Multi-Play Setups")
        L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
        L.append("    Key: MU=M Up  MD=M Down  BU=Breakout Up  BD=Breakout Down  SUP=Support  DB=Double Bottom")
        L.append("         5E=5 EMA  200=200 MA  E21=EMA21 Pullback  RSI=RSI Oversold  EBD=Beat Drift  EOR=Overreaction  EA=Anticipation")
        L.append("")
        for rank, (tkr, info) in enumerate(_confluence_list[:25], 1):
            price = info["price"]
            rs = info["rs_raw"]
            rsi = info.get("rsi")
            plays = info["plays"]
            play_codes = "+".join(_play_short.get(p, p) for p in plays)

            rsi_s = f"{rsi:.0f}" if rsi is not None else "--"
            rs_s = f"RS:{rs:+.1f}" if rs is not None else ""
            val_s = _val_str(tkr, info.get("value_grade"))
            earn_s = _earn_str(info.get("earnings"), info.get("revenue"))

            line = f"    #{rank:<2d}  {tkr:6s}  ${price:<9.2f}  ({len(plays)}x) {play_codes:20s}  RSI:{rsi_s:<5s}  {rs_s:9s}  {val_s}"
            if earn_s:
                line += f"  [{earn_s}]"
            L.append(line)
        L.append("")

    # --- EARNINGS DRIFT — Top 10 highest D (most opportunity) ---
    _drift_items = []
    for _lb in (all_leaderboard or []):
        _eps = (_lb.get("earnings") or {}).get("eps")
        if not _eps:
            continue
        _d = _eps.get("drift")
        if _d is not None and _d > 2:
            _drift_items.append((_lb, _eps, _d))
    _drift_items.sort(key=lambda x: -x[2])  # highest D first
    if _drift_items:
        _dr_n = min(len(_drift_items), 10)
        L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
        L.append(f"  ║    EARNINGS DRIFT — Top {_dr_n} Opportunities")
        L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
        L.append("    Exp = expected move (based on prev quarter's reaction).  D = Exp - actual move.")
        L.append("")
        for _dr_rank, (_dr_lb, _dr_eps, _dr_d) in enumerate(_drift_items[:10], 1):
            _dr_tkr = _dr_lb["ticker"]
            _dr_price = _dr_lb["price"]
            _dr_surp = _dr_eps.get("surprise_pct", 0)
            _dr_pchg = _dr_eps.get("price_chg_pct", 0)
            _dr_exp = _dr_eps.get("expected_move")
            _dr_tag = "Beat" if _dr_surp > 0 else "Miss"
            _dr_rs = _dr_lb.get("rs_raw")
            _dr_rs_s = f"RS:{_dr_rs:+.1f}" if _dr_rs is not None else ""
            _dr_rsi = _dr_lb.get("metrics", {}).get("rsi")
            _dr_rsi_s = f"{_dr_rsi:.0f}" if _dr_rsi is not None else "--"
            _dr_exp_s = f"Exp:{_dr_exp:+.1f}%" if _dr_exp is not None else ""
            _dr_d_s = f"D:{_dr_d:+.1f}" if _dr_d is not None else ""
            L.append(f"    #{_dr_rank:<2d}  {_dr_tkr:6s}  ${_dr_price:<9.2f}  {_dr_tag} {_dr_surp:+.1f}%  Stock:{_dr_pchg:+.1f}%  {_dr_exp_s}  {_dr_d_s}  RSI:{_dr_rsi_s:<5s}  {_dr_rs_s}")
        L.append("")

    # --- FEATURED PLAY SECTIONS ---
    for _fp_key, _fp_title, _fp_desc in _featured_plays:
        _fp_items = (signal_leaderboards or {}).get(_fp_key, [])
        if not _fp_items:
            continue
        _fp_n = min(len(_fp_items), 15)
        _ps = (play_stats or {}).get(_fp_key[0], {})
        _wr = f"  ({_ps['win_pct']}% win from {_ps['total']})" if _ps.get("total", 0) >= 5 else ""
        L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
        L.append(f"  ║    {_fp_title} — Top {_fp_n}{_wr}")
        L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
        L.append(f"    {_fp_desc}")
        L.append("")
        for rank, item in enumerate(_fp_items[:15], 1):
            tkr = item.get("ticker", "???")
            price = item.get("price", 0)
            rs = item.get("rs_raw")
            rsi = item.get("rsi")
            key_col = _key_col_for(_fp_key, item)

            rsi_s = f"{rsi:.0f}" if rsi is not None else "--"
            rs_s = f"RS:{rs:+.1f}" if rs is not None else ""
            val_s = _val_str(tkr, item.get("value_grade"))
            earn_s = _earn_str(item.get("earnings"), item.get("revenue"))

            L.append(_fmt_featured_line(rank, tkr, price, key_col, rsi_s, rs_s, val_s, earn_s))
        L.append("")

    # ----- PORTFOLIO WATCH — dedicated section for favorites -----
    _favorites = favorites or set()
    if _favorites:
        results_by_ticker = {}
        for r in results:
            # Keep highest-scoring result per ticker
            if r["ticker"] not in results_by_ticker or r["score"] > results_by_ticker[r["ticker"]]["score"]:
                results_by_ticker[r["ticker"]] = r
        dip_by_ticker = {}
        if dip_watch:
            for d in dip_watch:
                dip_by_ticker[d["ticker"]] = d

        pf_with_setup = []
        pf_dip_only = []
        pf_no_setup = []
        for tkr in sorted(_favorites):
            if tkr in results_by_ticker:
                pf_with_setup.append(results_by_ticker[tkr])
            elif tkr in dip_by_ticker:
                pf_dip_only.append(dip_by_ticker[tkr])
            else:
                pf_no_setup.append(tkr)
        pf_with_setup.sort(key=lambda r: r["score"], reverse=True)
        pf_dip_only.sort(key=lambda d: min(d["dips"].values()))

        L.append("  ================================================================")
        L.append(f"    PORTFOLIO WATCH — {len(_favorites)} holdings")
        L.append("  ================================================================")
        L.append("")
        for r in pf_with_setup:
            _tag = "LONG" if r["direction"] == "bullish" else "SHORT"
            _rs = r.get("rs_raw")
            _rs_str = f"RS:{_rs:+.1f}" if _rs is not None else ""
            _n_sigs = len(r.get("signals", []))
            _play = PLAY_TYPES.get(r.get("play_type", ""), r.get("play_type", ""))
            _flags = ""
            _sec_st = r.get("sector_status")
            if _sec_st == "leading":
                _flags += "  [LEAD]"
            elif _sec_st == "lagging":
                _flags += "  [LAG]"
            _earn = r.get("earnings", {})
            if _earn.get("days_until") is not None and _earn["days_until"] <= EARNINGS_NOTE_DAYS:
                _flags += f"  [EARN {_earn['days_until']}d]"
            if _earn.get("days_since") is not None:
                _flags += f"  [EARN -{_earn['days_since']}d]"
            L.append(f"    {r['ticker']:6s}  ${r['price']:<10.2f}  {r['score']:>4.1f}/10  {r['tier']}  {_tag}  {_n_sigs} sigs  {_rs_str}  [{_play}]{_flags}")
        for d in pf_dip_only:
            _dip_parts = []
            for ma_key in ["200", "21", "5"]:
                if ma_key in d["dips"]:
                    _dip_parts.append(f"{d['dips'][ma_key]:+.1f}% vs {ma_key} EMA")
            _dip_str = "  |  ".join(_dip_parts)
            L.append(f"    {d['ticker']:6s}  — no active setup —    DIP: {_dip_str}")
        for tkr in pf_no_setup:
            L.append(f"    {tkr:6s}  — no active setup —")
        L.append("")

    L.append("  ================================================================")
    L.append("    END OF REPORT")
    L.append("  ================================================================")
    L.append("")

    report_text = "\n".join(L)
    report_path.write_text(report_text, encoding="utf-8")
    return report_path, report_text


# ---------------------------------------------------------------------------
# Missed movers report — separate persistent file
# ---------------------------------------------------------------------------

def _format_mover_block(m: dict) -> list:
    """Format a single missed mover entry for the report."""
    lines = []
    direction = "UP" if m["move_3d"] > 0 else "DN"
    sig_detail = f"{m['signal_count']} sig"
    if m["signal_count"] > 0:
        sig_detail += f" ({m['bull_signals']}bull/{m['bear_signals']}bear)"
    else:
        sig_detail = "0 signals"
    # Earnings tag if available
    earn_tag = ""
    if m.get("earn_days_since") is not None and m["earn_days_since"] <= 10:
        earn_tag = f"  [EARN -{m['earn_days_since']}d]"
    elif m.get("earn_days_until") is not None and m["earn_days_until"] <= 5:
        earn_tag = f"  [EARN {m['earn_days_until']}d]"
    lines.append(f"    {m['ticker']:6s}  {direction} {m['move_3d']:+5.1f}% (3d)  {m['move_1d']:+5.1f}% (1d)  ${m['price']:<8.2f}  [{sig_detail}]{earn_tag}")

    fired = m.get("signals_fired", [])
    if fired:
        lines.append(f"             Signals: {', '.join(fired)}")

    fp = m.get("fingerprint", {})
    if fp:
        highlights = []
        if fp.get("obv_diverging_up"):
            highlights.append("OBV diverging up")
        if fp.get("obv_diverging_down"):
            highlights.append("OBV diverging down")
        if fp.get("accumulation_vol"):
            highlights.append(f"accum vol {fp.get('up_down_vol_ratio', 0):.1f}x")
        if fp.get("distribution_vol"):
            highlights.append(f"distrib vol {fp.get('up_down_vol_ratio', 0):.1f}x")
        if fp.get("tight_base"):
            highlights.append(f"tight base ({fp.get('consolidation_pct', 0):.0f}% range)")
        if fp.get("higher_lows"):
            highlights.append("higher lows")
        if fp.get("lower_highs"):
            highlights.append("lower highs")
        if fp.get("vol_expanding"):
            highlights.append(f"vol expanding {fp.get('vol_ratio_3d', 0):.1f}x")
        if fp.get("near_support"):
            highlights.append("near support")
        if fp.get("near_resistance"):
            highlights.append("near resistance")
        if fp.get("gap_recent"):
            highlights.append(f"recent gap ({fp.get('max_gap_pct', 0):.1f}%)")
        if fp.get("rsi_near_oversold"):
            highlights.append(f"RSI {fp.get('rsi', 0):.0f} (near oversold)")
        if fp.get("rsi_near_overbought"):
            highlights.append(f"RSI {fp.get('rsi', 0):.0f} (near overbought)")
        if highlights:
            lines.append(f"             Conditions: {', '.join(highlights)}")
    lines.append("")
    return lines


def write_missed_movers_report(missed_movers: list, pattern_gaps: dict,
                                regime: str = "unknown",
                                catalyst_movers: list = None):
    """Write detailed missed movers + signal gap analysis to a standalone report.
    Overwrites each scan so the file always shows the latest state.

    Missed movers are split into:
    - TRUE MISSES: system didn't detect the setup (low signal count)
    - SIGNAL CONFLICTS: system detected activity but signals contradicted
    - CATALYST EVENTS: excluded — gap + extreme volume = earnings/news event
    """
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / "missed_movers.txt"
    catalyst_movers = catalyst_movers or []

    L = []
    L.append("=" * 64)
    L.append("  MISSED MOVERS & SIGNAL GAP ANALYSIS")
    L.append(f"  {datetime.now().strftime('%A, %B %d %Y  %I:%M %p')}")
    L.append("=" * 64)
    L.append("")

    # Split into true misses vs signal conflicts
    true_misses = [m for m in missed_movers if m.get("miss_type") == "true_miss"]
    conflicts = [m for m in missed_movers if m.get("miss_type") == "conflict"]

    # --- TRUE MISSES (system didn't catch it) ---
    if true_misses:
        min_sigs = 5 if regime == "chop" else 4
        L.append(f"  TRUE MISSES — {len(true_misses)} big moves not detected")
        L.append(f"  (Fewer than {min_sigs} signals fired — genuine gaps in coverage)")
        L.append("-" * 64)
        L.append("")
        for m in true_misses:
            L.extend(_format_mover_block(m))
    else:
        L.append("  TRUE MISSES — none this scan")
        L.append("")

    # --- SIGNAL CONFLICTS (system detected but couldn't resolve direction) ---
    if conflicts:
        L.append("-" * 64)
        L.append(f"  SIGNAL CONFLICTS — {len(conflicts)} detected but bull/bear signals cancelled")
        L.append("  (System saw the activity but opposing signals blocked a pick)")
        L.append("-" * 64)
        L.append("")
        for m in conflicts:
            L.extend(_format_mover_block(m))

    # --- CATALYST EVENTS (excluded from analysis) ---
    if catalyst_movers:
        L.append("-" * 64)
        L.append(f"  CATALYST EVENTS (excluded) — {len(catalyst_movers)} gap + volume spike moves")
        L.append("  (2%+ gap with 2.5x+ volume = earnings/news, not a technical miss)")
        L.append("-" * 64)
        L.append("")
        for m in catalyst_movers:
            L.extend(_format_mover_block(m))
    elif not true_misses and not conflicts:
        L.append("  No missed movers this scan.")
        L.append("")

    # --- SIGNAL GAP ANALYSIS (accumulated across scans) ---
    if pattern_gaps and pattern_gaps.get("has_data"):
        up_gaps = pattern_gaps.get("up_gaps", [])
        dn_gaps = pattern_gaps.get("dn_gaps", [])

        L.append("=" * 64)
        L.append(f"  SIGNAL GAP ANALYSIS — {pattern_gaps['total_movers']} missed movers across {pattern_gaps.get('scan_count', '?')} scans")
        L.append("=" * 64)
        L.append("")
        L.append("  How to read: each row shows a technical condition found in missed movers.")
        L.append("  'Coverage' = which existing signal should catch this condition.")
        L.append("  Low fire rate = the signal exists but its thresholds are too strict.")
        L.append("  NONE = genuine gap — no signal covers this pattern.")
        L.append("")

        for direction_key, label, gaps, count in [
            ("up", "UP", up_gaps, pattern_gaps.get("up_count", 0)),
            ("dn", "DOWN", dn_gaps, pattern_gaps.get("dn_count", 0)),
        ]:
            if not gaps:
                continue
            L.append(f"  Missed {label} movers ({count}):")
            L.append(f"  {'Freq':>5s}  {'Condition':<45s}  {'Coverage':<30s}  {'Fire Rate':>9s}")
            L.append(f"  {'-'*5}  {'-'*45}  {'-'*30}  {'-'*9}")
            for g in gaps:
                coverage = ", ".join(g["coverage_signals"]) if g["coverage_signals"] else "NONE"
                if g["coverage_signals"]:
                    fire_str = f"{g['signal_fire_rate']}%"
                else:
                    fire_str = "—"
                L.append(f"  {g['frequency']:4.0f}%  {g['label']:<45s}  {coverage:<30s}  {fire_str:>9s}")
            L.append("")

        # Actionable insights
        all_gaps = up_gaps + dn_gaps
        strict_signals = [g for g in all_gaps if g["frequency"] >= 40 and g["coverage_signals"] and g["signal_fire_rate"] < 30]
        no_coverage = [g for g in all_gaps if g["frequency"] >= 40 and not g["coverage_signals"]]

        if strict_signals or no_coverage:
            L.append("-" * 64)
            L.append("  SUGGESTIONS")
            L.append("-" * 64)
            L.append("")
            for g in strict_signals:
                sigs = ", ".join(g["coverage_signals"])
                L.append(f"  RELAX: {g['label']}")
                L.append(f"         Present in {g['frequency']}% of missed movers, but {sigs} only fired {g['signal_fire_rate']}%.")
                L.append(f"         The signal thresholds may be too strict — consider loosening.")
                L.append("")
            for g in no_coverage:
                L.append(f"  NEW:   {g['label']}")
                L.append(f"         Present in {g['frequency']}% of missed movers. No existing signal covers this.")
                L.append(f"         Consider adding a dedicated signal for this condition.")
                L.append("")
    else:
        L.append("=" * 64)
        L.append("  SIGNAL GAP ANALYSIS")
        L.append("=" * 64)
        L.append("")
        L.append("  Not enough data yet. Need 10+ missed movers with fingerprints")
        L.append("  across multiple scans before patterns emerge.")
        L.append("")

    L.append("=" * 64)
    L.append("  END OF MISSED MOVERS REPORT")
    L.append("=" * 64)

    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Sector rotation report — separate persistent file
# ---------------------------------------------------------------------------

def write_sector_report(sector_rotation: dict, sector_changes: dict,
                        regime: str = "unknown", market_trends: dict = None,
                        rotation_divergence: dict = None):
    """Write sector rotation analysis to a standalone report.
    Overwrites each scan so the file always shows the latest state."""
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / "sector_rotation.txt"

    if market_trends is None:
        market_trends = {}

    L = []
    L.append("=" * 64)
    L.append("  SECTOR ROTATION REPORT")
    L.append(f"  {datetime.now().strftime('%A, %B %d %Y  %I:%M %p')}")
    L.append("=" * 64)
    L.append("")

    # Market context one-liner
    if market_trends:
        L.append(f"  Market:  {', '.join(f'{k} = {v.upper()}' for k, v in market_trends.items())}")
    L.append(f"  Regime:  {regime.upper()}")

    # Rotation divergence warning
    if rotation_divergence and rotation_divergence.get("divergent"):
        L.append(f"  !! ROTATION DIVERGENCE: {rotation_divergence['reason']}")
    L.append("")

    # --- SECTOR RANKING TABLE ---
    if not sector_rotation:
        L.append("  No sector rotation data available.")
        L.append("")
    else:
        ranked = sorted(sector_rotation.items(), key=lambda x: x[1].get("rank", 99))
        L.append("  SECTOR RANKINGS (vs SPY — 1d / 10d / 30d / composite)")
        L.append("-" * 64)
        L.append("")

        sc = sector_changes.get("changes", {}) if sector_changes else {}
        for sector, data in ranked:
            status = data.get("status", "neutral").upper()
            tag = f"  {status}" if status != "NEUTRAL" else ""
            # Rank change and momentum vs previous scan
            change_str = ""
            if sector in sc:
                rd = sc[sector]["rank_delta"]
                cd = sc[sector]["comp_delta"]
                if rd > 0:
                    change_str += f"  ^{rd}"
                elif rd < 0:
                    change_str += f"  v{abs(rd)}"
                if cd > 1.0:
                    change_str += " ACCEL"
                elif cd < -1.0:
                    change_str += " DECEL"
            L.append(f"    #{data['rank']:<3d} {sector:<25s} {data.get('ret_1d', 0):+5.1f}%  {data['ret_10d']:+5.1f}%  {data['ret_30d']:+5.1f}%  comp:{data['composite']:+5.1f}{tag}{change_str}")
        L.append("")

        # Sector status transitions
        transitions = sector_changes.get("transitions", []) if sector_changes else []
        if transitions:
            L.append("-" * 64)
            L.append("  STATUS TRANSITIONS")
            L.append("-" * 64)
            L.append("")
            for t in transitions:
                arrow = f"{t['from'].upper()} -> {t['to'].upper()}"
                if t["to"] == "leading":
                    L.append(f"    >>> {t['sector']}: {arrow} — new money flowing in")
                elif t["to"] == "lagging":
                    L.append(f"    >>> {t['sector']}: {arrow} — money rotating out")
                elif t["from"] == "leading":
                    L.append(f"    >>> {t['sector']}: {arrow} — lost leadership, watch for exit")
                elif t["from"] == "lagging":
                    L.append(f"    >>> {t['sector']}: {arrow} — no longer worst, potential rotation in")
            L.append("")

        # Previous scan comparison summary
        if sector_changes and sector_changes.get("has_prev"):
            prev_time = sector_changes.get("prev_time", "")
            ts_str = ""
            if prev_time:
                try:
                    prev_dt = datetime.fromisoformat(prev_time)
                    ts_str = f" (vs {prev_dt.strftime('%b %d %I:%M %p')})"
                except Exception:
                    pass

            movers_up = [(s, c) for s, c in sc.items() if c["rank_delta"] > 0]
            movers_dn = [(s, c) for s, c in sc.items() if c["rank_delta"] < 0]
            accel = [(s, c) for s, c in sc.items() if c["comp_delta"] > 1.0]
            decel = [(s, c) for s, c in sc.items() if c["comp_delta"] < -1.0]

            if movers_up or movers_dn or accel or decel:
                L.append("-" * 64)
                L.append(f"  CHANGES SINCE LAST SCAN{ts_str}")
                L.append("-" * 64)
                L.append("")
                if movers_up:
                    movers_up.sort(key=lambda x: x[1]["rank_delta"], reverse=True)
                    for s, c in movers_up:
                        L.append(f"    ^ {s:<25s} up {c['rank_delta']} rank(s)  (#{c['prev_rank']} -> #{sector_rotation[s]['rank']})")
                if movers_dn:
                    movers_dn.sort(key=lambda x: x[1]["rank_delta"])
                    for s, c in movers_dn:
                        L.append(f"    v {s:<25s} down {abs(c['rank_delta'])} rank(s)  (#{c['prev_rank']} -> #{sector_rotation[s]['rank']})")
                if accel:
                    L.append("")
                    L.append("  Accelerating (composite growing > +1.0):")
                    for s, c in accel:
                        L.append(f"    + {s:<25s} comp delta: {c['comp_delta']:+.1f}")
                if decel:
                    L.append("")
                    L.append("  Decelerating (composite shrinking > -1.0):")
                    for s, c in decel:
                        L.append(f"    - {s:<25s} comp delta: {c['comp_delta']:+.1f}")
                L.append("")
        elif not sector_changes or not sector_changes.get("has_prev"):
            L.append("  (First scan — no previous data to compare)")
            L.append("")

    L.append("=" * 64)
    L.append("  END OF SECTOR ROTATION REPORT")
    L.append("=" * 64)

    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Losers post-mortem report
# ---------------------------------------------------------------------------

def write_losers_report(analysis: dict):
    """Write losers post-mortem analysis to a standalone report."""
    path = REPORTS_DIR / "losers_analysis.txt"
    now = datetime.now()
    L = []

    total_losses = analysis["total_losses"]
    total_wins = analysis["total_wins"]
    total_scored = analysis["total_scored"]
    win_pct = total_wins / total_scored * 100 if total_scored > 0 else 0

    # --- Header ---
    L.append("=" * 64)
    L.append("  LOSERS POST-MORTEM ANALYSIS")
    L.append(f"  {now.strftime('%A, %B %d %Y  %I:%M %p')}")
    L.append("=" * 64)
    L.append("")
    L.append(f"  {total_scored} scored picks  |  {total_wins} wins ({win_pct:.0f}%)  |  {total_losses} losses ({100-win_pct:.0f}%)")

    by_w = analysis["by_window"]
    window_parts = []
    for w in ["1d", "5d", "10d"]:
        c = by_w.get(w, 0)
        if c > 0:
            window_parts.append(f"{w}: {c}")
    if window_parts:
        L.append(f"  Losses by window:  {' | '.join(window_parts)}")
    L.append("")

    if analysis.get("sample_warning"):
        L.append(f"  * Small sample ({total_scored} picks) — patterns may not hold. Review again at 30+.")
        L.append("")

    # --- Section 1: Loser Details (sorted by tier, then TP1 progress) ---
    L.append("-" * 64)
    L.append("  LOSER DETAILS (A/B tiers only — C-tier watchlist excluded)")
    L.append("-" * 64)
    L.append("")

    tier_order = {"A": 0, "B": 1}
    sorted_losers = sorted(
        analysis["losers"],
        key=lambda x: (tier_order.get(x["tier"], 9), -x["failure"]["tp1_progress_pct"]),
    )

    current_tier = None
    for loser in sorted_losers:
        # Tier sub-header
        if loser["tier"] != current_tier:
            current_tier = loser["tier"]
            tier_losers = [l for l in sorted_losers if l["tier"] == current_tier]
            L.append(f"  --- {current_tier}-TIER LOSSES ({len(tier_losers)}) ---")
            L.append("")

        t = loser["ticker"]
        d = "LONG" if loser["direction"] == "bullish" else "SHORT"
        play = PLAY_TYPES.get(loser["play_type"], loser["play_type"])
        fail = loser["failure"]
        exit_p = loser["exit_price"]
        exit_str = f"${exit_p:.2f}" if exit_p else "n/a"

        L.append(f"    {t:6s}  {d}  ${loser['entry']:.2f} -> {exit_str} ({loser['window']})  [{play}]")
        L.append(f"            Entry: ${loser['entry']:.2f}  Stop: ${loser['stop']:.2f}  TP1: ${loser['tp1']:.2f}  Score: {loser['score']:.1f} {loser['tier']}-TIER")

        sigs = loser["signal_indicators"]
        if sigs:
            L.append(f"            Signals: {', '.join(sigs)}")

        L.append(f"            Failure: {fail['desc']}")
        causes = fail.get("causes", [])
        if causes:
            for cause in causes:
                L.append(f"            Why: {cause}")
        L.append(f"            TP1 Progress: {fail['tp1_progress_pct']:.0f}%{'  << almost worked' if fail['near_miss'] else ''}")

        ctx_parts = []
        if loser["regime"] != "unknown":
            ctx_parts.append(f"Regime: {loser['regime'].upper()}")
        if loser.get("sector_status") and loser["sector_status"] != "unknown":
            ctx_parts.append(f"Sector: {loser['sector_status'].upper()}")
        if loser["vol_ratio"]:
            ctx_parts.append(f"Vol: {loser['vol_ratio']:.1f}x")
        if ctx_parts:
            L.append(f"            Context: {', '.join(ctx_parts)}")
        L.append("")

    # --- Section 2: Patterns ---
    L.append("-" * 64)
    L.append("  PATTERNS IN LOSSES")
    L.append("-" * 64)
    L.append("")

    pats = analysis["patterns"]

    L.append("  By Failure Mode:")
    for mode, count in pats["by_failure_mode"].most_common():
        labels = {
            "GAP_THROUGH_STOP": "gapped through stop",
            "NEAR_MISS": "almost hit TP1, reversed",
            "IMMEDIATE_REVERSAL": "never moved toward target",
            "DRIFT_TO_STOP": "drifted to stop",
        }
        L.append(f"    {count}x  {labels.get(mode, mode)}")
    L.append("")

    if len(pats["by_play_type"]) > 1:
        L.append("  By Play Type:")
        for pt, count in pats["by_play_type"].most_common():
            L.append(f"    {count}x  {PLAY_TYPES.get(pt, pt)}")
        L.append("")

    if len(pats["by_tier"]) > 1:
        L.append("  By Tier:")
        for tier, count in pats["by_tier"].most_common():
            L.append(f"    {count}x  {tier}-TIER")
        L.append("")

    # --- ROOT CAUSES ---
    by_cause = pats.get("by_cause", {})
    if by_cause:
        L.append("-" * 64)
        L.append("  ROOT CAUSES — why trades failed")
        L.append("-" * 64)
        L.append("")
        total_losses = analysis["total_losses"]
        for cause, count in sorted(by_cause.items(), key=lambda x: -x[1]):
            pct = count / total_losses * 100 if total_losses > 0 else 0
            L.append(f"    {count:2d}x ({pct:2.0f}%)  {cause}")
        L.append("")

    # --- Section 3: Near Misses ---
    L.append("-" * 64)
    L.append("  NEAR MISSES — losers that almost hit TP1")
    L.append("-" * 64)
    L.append("")

    near = analysis["near_misses"]
    if near:
        L.append("  These picks reached 75%+ of the way to TP1 before reversing.")
        L.append("  Consider: are stops too tight? Would wider stops save these trades?")
        L.append("")
        for nm in sorted(near, key=lambda x: (tier_order.get(x["tier"], 9), -x["failure"]["tp1_progress_pct"])):
            t = nm["ticker"]
            d = "LONG" if nm["direction"] == "bullish" else "SHORT"
            prog = nm["failure"]["tp1_progress_pct"]
            is_long = nm["direction"] == "bullish"
            best = nm.get("high_since", nm["entry"]) if is_long else nm.get("low_since", nm["entry"])
            risk = abs(nm["entry"] - nm["stop"])
            remain = abs(nm["tp1"] - best) if best else 0
            L.append(f"    {t:6s}  {d}  reached {prog:.0f}% of TP1 (${nm['tp1']:.2f})  best: ${best:.2f}")
            L.append(f"            Entry: ${nm['entry']:.2f}  Stop: ${nm['stop']:.2f}  Risk: ${risk:.2f}  |  Remaining to TP1: ${remain:.2f}")
            L.append("")
    else:
        L.append("  (No near misses — stops don't appear too tight)")
        L.append("")

    # --- Section 4: Signal Toxicity ---
    L.append("-" * 64)
    L.append("  SIGNAL TOXICITY — signals appearing more on losers")
    L.append("-" * 64)
    L.append("")

    toxic = [s for s in analysis["signal_toxicity"] if s["loser_rate"] > 50 or s["on_losers"] >= 3]
    if toxic:
        L.append("  Signal                     Losers  Winners  Loser%  Total")
        L.append("  -------------------------  ------  -------  ------  -----")
        for s in toxic[:10]:
            L.append(f"  {s['signal']:27s}  {s['on_losers']:5d}  {s['on_winners']:7d}  {s['loser_rate']:5.0f}%  {s['total']:5d}")
        L.append("")
    else:
        L.append("  (No signals disproportionately on losers yet)")
        L.append("")

    # --- Section 5: Regime & Sector ---
    L.append("-" * 64)
    L.append("  REGIME & SECTOR BREAKDOWN")
    L.append("-" * 64)
    L.append("")

    rlr = analysis["regime_loss_rates"]
    if rlr:
        L.append("  Loss Rate by Regime:")
        for regime in ["trend", "range", "chop", "unknown"]:
            if regime in rlr:
                r = rlr[regime]
                total = r["wins"] + r["losses"]
                flag = "  << watch out" if r["rate"] > 60 and total >= 3 else ""
                L.append(f"    {regime.upper():8s}  {r['losses']} losses / {total} total = {r['rate']:.0f}%{flag}")
        L.append("")

    slr = analysis["sector_loss_rates"]
    if slr:
        L.append("  Loss Rate by Sector Status:")
        for ss in ["leading", "lagging", "unknown"]:
            if ss in slr:
                r = slr[ss]
                total = r["wins"] + r["losses"]
                flag = "  << watch out" if r["rate"] > 60 and total >= 3 else ""
                L.append(f"    {ss.upper():10s}  {r['losses']} losses / {total} total = {r['rate']:.0f}%{flag}")
        L.append("")

    # --- Footer ---
    L.append("=" * 64)
    L.append("  END OF LOSERS POST-MORTEM")
    L.append("=" * 64)

    path.write_text("\n".join(L), encoding="utf-8")
    return path
