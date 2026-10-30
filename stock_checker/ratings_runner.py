"""Ratings-based stock report runner.

Fetches current prices for user-rated stocks, compares to buy/sell targets,
and generates a report with value grades, sector rotation, and earnings data.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .constants import (USER_RATINGS_FILE, REPORTS_DIR,
                        NEAR_PCT, NEAR_PCT_BY_RATING)
from .data import (
    load_config, fetch_stock_data, get_live_price,
    get_fundamentals,
    reset_api_counter, get_api_call_count,
)
from .market import (
    calculate_sector_rotation, check_sector_changes,
    check_earnings_proximity, fetch_earnings_calendar,
    reset_earnings_calendar_cache, get_market_trend,
    get_market_context,
)
from .scoring import score_value
from .darkpool import score_all as score_dark_pool
from .report import write_sector_report
from .mutes import load_mutes, is_muted
from .alert_state import (load_state, save_state, mark_escalations,
                          record, ALERT_STATE_FILE)

import json

try:
    from db.writer import record_run, record_dark_pool
except Exception:  # the Postgres layer is optional - report runs without it
    def record_run(**_kwargs):
        pass

    def record_dark_pool(_history):
        pass

# 1-star rungs in APPROACHING are the noisiest bucket - the shallowest level
# on every ticker, all at once. Set False to bring them back.
HIDE_ONE_STAR_APPROACHING = True


def load_ratings() -> dict:
    if USER_RATINGS_FILE.exists():
        try:
            return json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _fetch_ticker_data(ticker: str, earnings_calendar: dict) -> dict:
    """Fetch current price, earnings, and fundamentals for a single ticker."""
    result = {"ticker": ticker, "price": None, "earnings": None, "value": None}

    # Current price — use last close (reliable), skip Alpaca (stale after hours)
    price = None
    df = fetch_stock_data(ticker, period="5d")
    if not df.empty:
        price = float(df["Close"].iloc[-1])
    result["price"] = price

    # Earnings proximity
    result["earnings"] = check_earnings_proximity(
        ticker, earnings_calendar=earnings_calendar
    )

    # Fundamentals for value grade
    fundamentals = get_fundamentals(ticker)
    if fundamentals:
        result["value"] = score_value(fundamentals, "bullish")

    return result


def _deepest_only(alerts: list) -> list:
    """Keep just the deepest triggered rung per ticker.

    Through rung 3 means rungs 1 and 2 are through as well; listing all three
    repeats one fact. The highest rating is the informative one - it is the
    commitment level the price has reached.
    """
    best = {}
    for a in alerts:
        cur = best.get(a["ticker"])
        if cur is None or a["rating"] > cur["rating"]:
            best[a["ticker"]] = a
    return list(best.values())


def _nearest_only(alerts: list) -> list:
    """Keep just the nearest untriggered rung per ticker.

    The mirror of _deepest_only. Price cannot reach rung 2 without passing
    rung 1, so the closest rung is the only actionable one; the rest just
    repeat "there is more below", which the (n/m) label already says.

    5-stars are exempt. A watch price is one the user chose deliberately, so
    it shows in addition to the generated rungs rather than competing with
    them - otherwise a nearer auto rung would silently hide it.
    """
    watches = [a for a in alerts if a["rating"] == 5]
    best = {}
    for a in alerts:
        if a["rating"] == 5:
            continue
        cur = best.get(a["ticker"])
        if cur is None or a["pct_away"] < cur["pct_away"]:
            best[a["ticker"]] = a
    return watches + list(best.values())


def _classify_entry(current_price: float, entry: dict, side: str) -> dict:
    """Classify how close the current price is to a buy/sell target."""
    target = entry["price"]
    rating = entry["rating"]

    if side == "buy":
        pct_away = (current_price - target) / target * 100
        triggered = current_price <= target
    else:
        pct_away = (target - current_price) / target * 100
        triggered = current_price >= target

    return {
        "target": target,
        "rating": rating,
        "pct_away": round(pct_away, 1),
        "triggered": triggered,
    }


def run_report() -> tuple:
    """Run the ratings-based report. Returns (report_path, report_text)."""
    ratings = load_ratings()
    if not ratings:
        print("  No ratings found. Run rate_stocks.py first.")
        return None, None

    run_started_at = datetime.now(timezone.utc)
    config = load_config()
    reset_api_counter()
    reset_earnings_calendar_cache()

    rated_tickers = list(ratings.keys())
    print(f"\n  Fetching data for {len(rated_tickers)} rated stocks...")

    # Prefetch earnings calendar
    earnings_calendar = fetch_earnings_calendar()

    # Fetch prices + earnings + fundamentals in parallel
    ticker_data = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_ticker_data, t, earnings_calendar): t
            for t in rated_tickers
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            ticker = futures[future]
            try:
                ticker_data[ticker] = future.result()
            except Exception as e:
                print(f"  [ERROR] {ticker}: {e}")
                ticker_data[ticker] = {"ticker": ticker, "price": None, "earnings": None, "value": None}
            if done % 25 == 0:
                print(f"  ... {done}/{len(rated_tickers)} tickers fetched")

    print(f"  All {len(rated_tickers)} tickers fetched. ({get_api_call_count()} API calls)")

    # Dark pool score - FINRA's daily off-exchange file, a day behind
    print("  Fetching dark pool data...")
    dark_pool, dp_history = score_dark_pool(rated_tickers)
    dp_as_of = max((d["as_of"] for d in dark_pool.values()), default=None)

    # Sector rotation
    print("  Fetching sector rotation...")
    spy_df = fetch_stock_data("SPY", period="6mo")
    sector_rotation = calculate_sector_rotation(spy_df)
    spy_close = float(spy_df["Close"].iloc[-1]) if not spy_df.empty else 0
    sector_changes = check_sector_changes(sector_rotation, spy_close=spy_close)

    # Market trends
    market_trends = get_market_trend(config)
    market_context = get_market_context()

    mutes = load_mutes()

    # Build buy and sell lists (triggered + within 5% only)
    buy_alerts = []
    buy_near = []
    sell_alerts = []
    sell_near = []

    # Every rung evaluated this run, before any report filtering. The
    # report only shows the deepest/nearest unmuted rungs; the database
    # keeps the full picture so history is complete.
    all_evals = []

    for ticker, data in ratings.items():
        td = ticker_data.get(ticker, {})
        price = td.get("price")
        if price is None:
            continue

        earnings = td.get("earnings", {})
        value = td.get("value", {})

        # Process buys
        for entry in data.get("buys", []):
            info = _classify_entry(price, entry, "buy")
            all_evals.append((ticker, "buy", info, price))
            item = {
                "ticker": ticker,
                "price": price,
                "earnings": earnings,
                "value": value,
                "dp": dark_pool.get(ticker),
                # How many rungs this ticker has, so the report can say
                # (2/4) on a four-rung ladder instead of always (2/3)
                "rungs": len(data.get("buys", [])),
                # $ marks a ladder auto-filled and not yet reviewed
                "auto": data.get("source") == "auto",
                **info,
            }
            if is_muted(mutes, ticker, info["rating"], "buy"):
                continue  # you already passed on this rung
            near_pct = NEAR_PCT_BY_RATING.get(info["rating"], NEAR_PCT)
            if info["triggered"]:
                buy_alerts.append(item)
            elif info["pct_away"] <= near_pct:
                if not (HIDE_ONE_STAR_APPROACHING and info["rating"] == 1):
                    buy_near.append(item)

        # Process sells
        for entry in data.get("sells", []):
            info = _classify_entry(price, entry, "sell")
            all_evals.append((ticker, "sell", info, price))
            item = {
                "ticker": ticker,
                "price": price,
                "earnings": earnings,
                "value": value,
                "dp": dark_pool.get(ticker),
                "rungs": len(data.get("sells", [])),
                "auto": data.get("sells_source") == "auto",
                **info,
            }
            if is_muted(mutes, ticker, info["rating"], "sell"):
                continue
            near_pct = NEAR_PCT_BY_RATING.get(info["rating"], NEAR_PCT)
            if info["triggered"]:
                sell_alerts.append(item)
            elif info["pct_away"] <= near_pct:
                sell_near.append(item)

    # If price is through several rungs, only the deepest one says anything -
    # it carries the commitment level. The shallower rungs are the same news.
    buy_alerts = _deepest_only(buy_alerts)
    sell_alerts = _deepest_only(sell_alerts)
    buy_near = _nearest_only(buy_near)
    sell_near = _nearest_only(sell_near)

    # Flag rungs that are new or deeper than the last report. Seed silently on
    # the first ever run so the whole book does not light up at once.
    state = load_state()
    seed = not ALERT_STATE_FILE.exists()
    # The near lists are tracked the same way - a name newly inside range, or
    # inside range on a deeper rung than before, is the news there.
    mark_escalations(buy_alerts, state, "buy", seed)
    mark_escalations(sell_alerts, state, "sell", seed)
    mark_escalations(buy_near, state, "buy_near", seed)
    mark_escalations(sell_near, state, "sell_near", seed)
    record(buy_alerts, state, "buy")
    record(sell_alerts, state, "sell")
    record(buy_near, state, "buy_near")
    record(sell_near, state, "sell_near")
    save_state(state)

    # Sort both sections by rating first — a deep rung being approached is
    # bigger news than a shallow one being closer. Distance breaks ties.
    # Rung first, then how far past the target within each rung - so a 3-star
    # never sorts below a 1-star, but depth orders the ones that tie.
    buy_alerts.sort(key=lambda x: (-x["rating"], x["pct_away"]))
    sell_alerts.sort(key=lambda x: (-x["rating"], -x["pct_away"]))
    buy_near.sort(key=lambda x: (-x["rating"], x["pct_away"]))
    sell_near.sort(key=lambda x: (-x["rating"], x["pct_away"]))

    # Generate report
    path, text = write_ratings_report(
        buy_alerts=buy_alerts,
        buy_near=buy_near,
        sell_alerts=sell_alerts,
        sell_near=sell_near,
        sector_rotation=sector_rotation,
        sector_changes=sector_changes,
        market_trends=market_trends,
        market_context=market_context,
        dp_as_of=dp_as_of,
    )

    # Also write sector report
    write_sector_report(sector_rotation, sector_changes,
                        market_trends=market_trends)

    print(f"\n  Report saved: {path}")
    print(f"  API calls: {get_api_call_count()}")

    # Persist the run. Additive and fail-soft: the JSON state files above
    # are already written, and a database problem prints one line rather
    # than breaking the report.
    record_run(
        evaluations=all_evals,
        ticker_data=ticker_data,
        report_lists={"buy": buy_alerts, "sell": sell_alerts,
                      "buy_near": buy_near, "sell_near": sell_near},
        sector_rotation=sector_rotation,
        spy_close=spy_close,
        market_trends=market_trends,
        report_path=path,
        started_at=run_started_at,
        api_calls=get_api_call_count(),
    )
    record_dark_pool(dp_history)

    return path, text


def _earn_tag(earnings: dict) -> str:
    """Format earnings proximity as a compact tag."""
    if not earnings:
        return ""
    du = earnings.get("days_until")
    ds = earnings.get("days_since")
    parts = []
    if du is not None:
        parts.append(f"EARN +{du}d")
    if ds is not None:
        parts.append(f"EARN -{ds}d")
    return "  ".join(parts)


def _val_tag(value: dict) -> str:
    """Format value grade as a compact tag."""
    if not value or not value.get("grade") or value["grade"] == "N/A":
        return ""
    grade = value["grade"]
    score = value.get("score", 0)
    details = value.get("details", {})
    parts = [f"Val:{grade}({score})"]
    for key in ("valuation", "profitability", "health", "growth"):
        detail = details.get(key, "")
        if detail and detail != "no data":
            parts.append(detail)
    return " | ".join(parts)


def _dp_tag(dp: dict | None) -> str:
    """Dark pool score as a compact tag. 5 is normal, 10 hidden buying."""
    if not dp:
        return ""
    return f"DP {dp['score']}"


def _fmt_item(item: dict, side: str) -> str:
    """Format a single buy/sell item for the report."""
    ticker = item["ticker"]
    price = item["price"]
    target = item["target"]
    rating = item["rating"]
    pct = item["pct_away"]
    rungs = max(item.get("rungs", 3), rating)
    stars = "*" * rating

    if side == "buy":
        if item["triggered"]:
            status = f"{abs(pct):.1f}% BELOW ${target:.2f}"
        else:
            status = f"{pct:.1f}% above ${target:.2f}"
    else:
        if item["triggered"]:
            status = f"{abs(pct):.1f}% ABOVE ${target:.2f}"
        else:
            status = f"{pct:.1f}% below ${target:.2f}"

    earn = _earn_tag(item.get("earnings", {}))
    val = _val_tag(item.get("value", {}))

    tags = []
    if earn:
        tags.append(earn)
    dp = _dp_tag(item.get("dp"))
    if dp:
        tags.append(dp)
    if val:
        tags.append(val)
    tag_str = f"  [{' | '.join(tags)}]" if tags else ""

    ticker = ticker + "$" if item.get("auto") else ticker
    ticker = ("*" + ticker) if item.get("escalated") else (" " + ticker)
    return f"   {ticker:7s}  ${price:<9.2f}  {status:30s}  {stars:{max(5, rungs)}s} ({rating}/{rungs}){tag_str}"


def _escalated_only(items: list) -> list:
    """Keep only the flagged lines - what is new or deeper since last report.

    Everything else is the same news as yesterday, and at a few hundred lines a
    run the unchanged names bury the ones that moved.
    """
    return [i for i in items if i.get("escalated")]


def write_ratings_report(buy_alerts, buy_near,
                          sell_alerts, sell_near,
                          sector_rotation, sector_changes,
                          market_trends, market_context,
                          dp_as_of=None) -> tuple:
    """Write the ratings-based report - flagged lines only."""
    totals = (len(buy_alerts), len(buy_near), len(sell_alerts), len(sell_near))
    buy_total, buy_near_total, sell_total, sell_near_total = totals
    buy_alerts = _escalated_only(buy_alerts)
    buy_near = _escalated_only(buy_near)
    sell_alerts = _escalated_only(sell_alerts)
    sell_near = _escalated_only(sell_near)

    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = REPORTS_DIR / f"ratings_{timestamp}.txt"

    L = []
    L.append("")
    L.append("  ================================================================")
    L.append("    STOCK RATINGS REPORT")
    L.append(f"    {datetime.now().strftime('%A, %B %d %Y  %I:%M %p')}")
    L.append("  ================================================================")
    L.append("")

    # Market context
    if market_trends:
        L.append(f"    Market:   {', '.join(f'{k} = {v.upper()}' for k, v in market_trends.items())}")
    if market_context:
        ctx_parts = []
        for sym, info in market_context.items():
            trend = info["trend"].upper()
            pct = info.get("pct_above_200", 0)
            ctx_parts.append(f"{info['label']} = {trend} ({pct:+.1f}%)")
        L.append(f"    Context:  {', '.join(ctx_parts)}")
    if dp_as_of:
        # FINRA posts each file in the evening, so this trails by a day
        L.append(f"    Dark pool: FINRA off-exchange as of {dp_as_of.strftime('%a %b %d')}"
                 f"  (DP 5 = normal, 10 = hidden buying, 1 = hidden selling)")
    L.append("")

    # ==================== BUYS ====================
    L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
    L.append("  ║    B U Y S")
    L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
    L.append("")

    # A 5-star closing in is the most actionable line in the report - a price
    # you chose deliberately and are waiting on. It goes above everything.
    watch_near = [i for i in buy_near if i["rating"] == 5]
    rest_near = [i for i in buy_near if i["rating"] != 5]

    if watch_near:
        L.append(f"  >>> WATCH CLOSING IN — {len(watch_near)} new within "
                 f"{NEAR_PCT_BY_RATING[5]:.1f}% of a 5-star price <<<")
        L.append("")
        for item in watch_near:
            L.append(_fmt_item(item, "buy"))
        L.append("")

    if buy_alerts:
        L.append(f"  *** TRIGGERED — {len(buy_alerts)} new of {buy_total} "
                 f"at or below your buy price ***")
        L.append("")
        for item in buy_alerts:
            L.append(_fmt_item(item, "buy"))
        L.append("")

    if rest_near:
        L.append(f"  APPROACHING — {len(rest_near)} new of {buy_near_total} "
                 f"within {NEAR_PCT:.0f}% of buy price")
        L.append("")
        for item in rest_near:
            L.append(_fmt_item(item, "buy"))
        L.append("")

    if not buy_alerts and not buy_near:
        if buy_total or buy_near_total:
            L.append(f"    Nothing new on the buy side — {buy_total} triggered and "
                     f"{buy_near_total} approaching, all unchanged since last report.")
        else:
            L.append("    Nothing near your buy targets.")
        L.append("")

    # ==================== SELLS ====================
    L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
    L.append("  ║    S E L L S")
    L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
    L.append("")

    if sell_alerts:
        L.append(f"  *** TRIGGERED — {len(sell_alerts)} new of {sell_total} "
                 f"at or above your sell price ***")
        L.append("")
        for item in sell_alerts:
            L.append(_fmt_item(item, "sell"))
        L.append("")

    if sell_near:
        L.append(f"  APPROACHING — {len(sell_near)} new of {sell_near_total} "
                 f"within 5% of sell price")
        L.append("")
        for item in sell_near:
            L.append(_fmt_item(item, "sell"))
        L.append("")

    if not sell_alerts and not sell_near:
        if sell_total or sell_near_total:
            L.append(f"    Nothing new on the sell side — {sell_total} triggered and "
                     f"{sell_near_total} approaching, all unchanged since last report.")
        else:
            L.append("    Nothing near your sell targets.")
        L.append("")

    # ==================== SECTOR ROTATION ====================
    if sector_rotation:
        sc = sector_changes.get("changes", {}) if sector_changes else {}

        L.append("  ╔══════════════════════════════════════════════════════════════════════════════════════════╗")
        L.append("  ║    SECTOR ROTATION (vs SPY)")
        L.append("  ╚══════════════════════════════════════════════════════════════════════════════════════════╝")
        L.append("")

        ranked = sorted(sector_rotation.items(), key=lambda x: x[1].get("rank", 99))
        for sector, data in ranked:
            status = data.get("status", "neutral").upper()
            tag = f"  {status}" if status != "NEUTRAL" else ""
            change_str = ""
            if sector in sc:
                rd = sc[sector]["rank_delta"]
                if rd > 0:
                    change_str += f"  ^{rd}"
                elif rd < 0:
                    change_str += f"  v{abs(rd)}"
            L.append(f"    #{data['rank']:<3d} {sector:<25s} {data.get('ret_1d', 0):+5.1f}%  {data['ret_10d']:+5.1f}%  {data['ret_30d']:+5.1f}%  comp:{data['composite']:+5.1f}{tag}{change_str}")
        L.append("")

        transitions = sector_changes.get("transitions", []) if sector_changes else []
        if transitions:
            parts = []
            for t in transitions:
                parts.append(f"{t['sector']}: {t['from'].upper()} -> {t['to'].upper()}")
            L.append(f"    Transitions: {' | '.join(parts)}")
            L.append("")

    # ==================== FOOTER ====================
    L.append("  ================================================================")
    L.append("    END OF REPORT")
    L.append("  ================================================================")
    L.append("")

    report_text = "\n".join(L)
    report_path.write_text(report_text, encoding="utf-8")
    return report_path, report_text


def main():
    print("=" * 60)
    print("  STOCK RATINGS REPORT")
    print("=" * 60)

    path, text = run_report()
    if text:
        print(text)
