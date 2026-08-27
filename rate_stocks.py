"""Interactive stock rating tool.

Walk through tickers and set personal buy or sell price points.
Entry 1 = rating 1, entry 2 = rating 2, entry 3 = rating 3 (automatic).
Progress is saved after every stock so you can quit and resume anytime.

Usage:
    python rate_stocks.py buy           # rate buy entries (default if omitted)
    python rate_stocks.py sell          # rate sell entries
    python rate_stocks.py buy --refresh     # re-rate oldest first
    python rate_stocks.py buy --refresh 30  # only ratings older than 30 days
    python rate_stocks.py buy --restart     # start from the beginning
    python rate_stocks.py buy --only AAPL MSFT  # rate specific tickers
    python rate_stocks.py --edit AAPL MSFT  # edit specific tickers (buy + sell)
    python rate_stocks.py --skip AVGO       # snooze 14 days (let a move settle)
    python rate_stocks.py --skip AVGO 30    # snooze a custom number of days
    python rate_stocks.py --watch AZO 2500  # 5-star: the price you are waiting for
    python rate_stocks.py --watch AZO       # show / clear with --unwatch
    python rate_stocks.py --unwatch AZO     # drop the 5-star level
    python rate_stocks.py --mute AVGO 2     # passed on rung 2 - hide it 14 days
    python rate_stocks.py --mute AVGO       # hide every AVGO buy rung
    python rate_stocks.py --mute AVGO 2 30  # custom number of days
    python rate_stocks.py --mutes           # list what is muted
    python rate_stocks.py --unmute AVGO     # un-mute a ticker
    python rate_stocks.py --skips           # list what is snoozed
    python rate_stocks.py --unskip AVGO     # bring one back now
    python rate_stocks.py --show            # show all current ratings
    python rate_stocks.py --show AAPL       # show rating for one ticker
    python rate_stocks.py --delete AAPL     # delete a bad rating
"""

import json
import sys
from datetime import date, timedelta

from stock_checker.constants import (USER_RATINGS_FILE, ROOT_DIR,
                                     SKIPPED_FILE, SKIP_DAYS_DEFAULT,
                                     NEAR_PCT, NEAR_PCT_BY_RATING)
from stock_checker.mutes import (load_mutes, save_mutes, add_mute,
                                 show_mutes, MUTE_DAYS_DEFAULT)

CONFIG_FILE = ROOT_DIR / "config.json"


def load_ratings() -> dict:
    if USER_RATINGS_FILE.exists():
        try:
            return json.loads(USER_RATINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_ratings(ratings: dict):
    USER_RATINGS_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")


def load_tickers() -> list:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return config.get("tickers", [])


def _days_since(date_str: str) -> int:
    try:
        return (date.today() - date.fromisoformat(date_str)).days
    except Exception:
        return 9999


def load_skips() -> dict:
    """Snoozed tickers, with anything past its date already dropped."""
    if not SKIPPED_FILE.exists():
        return {}
    try:
        raw = json.loads(SKIPPED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    today = date.today().isoformat()
    live = {t: v for t, v in raw.items() if v.get("until", "") > today}
    if len(live) != len(raw):
        save_skips(live)  # expired ones age out on their own
    return live


def save_skips(skips: dict):
    SKIPPED_FILE.write_text(json.dumps(skips, indent=2), encoding="utf-8")


def add_skip(skips: dict, ticker: str, days: int = SKIP_DAYS_DEFAULT,
             note: str = "") -> str:
    until = (date.today() + timedelta(days=days)).isoformat()
    skips[ticker] = {"since": date.today().isoformat(), "until": until}
    if note:
        skips[ticker]["note"] = note
    return until


def show_skips(skips: dict):
    if not skips:
        print("\n  Nothing skipped.\n")
        return
    print(f"\n  {'TICKER':8s}  {'SKIPPED':12s}  {'RETURNS':12s}  {'DAYS LEFT':9s}  NOTE")
    print(f"  {'------':8s}  {'-------':12s}  {'-------':12s}  {'---------':9s}  ----")
    for t in sorted(skips, key=lambda t: skips[t].get("until", "")):
        v = skips[t]
        left = -_days_since(v.get("until", ""))
        print(f"  {t:8s}  {v.get('since','-'):12s}  {v.get('until','-'):12s}"
              f"  {left:<9d}  {v.get('note','')}")
    print(f"\n  {len(skips)} skipped.\n")


def show_ratings(ratings: dict, tickers: list = None):
    if not ratings:
        print("\n  No ratings yet.\n")
        return

    items = ratings if tickers is None else {t: ratings[t] for t in tickers if t in ratings}
    if not items:
        print("\n  No ratings found for those tickers.\n")
        return

    # Widen to however many rungs actually exist, never narrower than 3, so
    # the table is unchanged unless something has a 4th entry.
    n_buy = max(3, max((len(d.get("buys", [])) for d in items.values()), default=3))
    n_sell = max(3, max((len(d.get("sells", [])) for d in items.values()), default=3))

    head = f"\n  {'TICKER':8s}  {'UPDATED':10s}"
    rule = f"  {'------':8s}  {'-------':10s}"
    for i in range(1, n_buy + 1):
        head += f"  {'BUY ' + str(i):12s}"
        rule += f"  {'-----':12s}"
    for i in range(1, n_sell + 1):
        head += f"  {'SELL ' + str(i):12s}"
        rule += f"  {'------':12s}"
    print(head)
    print(rule)

    for ticker in sorted(items):
        data = items[ticker]
        # $ marks a ladder filled in automatically that you have not reviewed
        label = ticker + ("$" if data.get("source") == "auto" else "")
        updated = data.get("updated", "—")
        buys = data.get("buys", [])
        sells = data.get("sells", [])
        bp = [f"${e['price']:.2f} ({e['rating']})" if e.get("price") else "--" for e in buys]
        while len(bp) < n_buy:
            bp.append("--")
        sp = [f"${e['price']:.2f} ({e['rating']})" if e.get("price") else "--" for e in sells]
        while len(sp) < n_sell:
            sp.append("--")
        row = f"  {label:8s}  {updated:10s}"
        for cell in bp[:n_buy] + sp[:n_sell]:
            row += f"  {cell:12s}"
        print(row)
    print(f"\n  {len(items)} stocks rated.\n")


def get_price(prompt: str) -> float:
    """Get a price from user. Returns None if skipped. Returns 'q' to quit."""
    while True:
        val = input(prompt).strip()
        if val == "":
            return None
        if val.lower() == "q":
            return "q"
        if val.lower() == "s":
            return "s"
        val = val.lstrip("$")
        try:
            price = float(val)
            if price > 0:
                return price
            print("      Must be positive.")
        except ValueError:
            print("      Enter a number, Enter to skip, q to quit.")


def rate_side(ticker: str, side: str) -> list:
    """Collect up to 3 prices for one side. Rating = entry number (1/2/3).
    Enter to skip an entry. Returns list of entries or empty list."""
    entries = []
    for i in range(1, 4):
        price = get_price(f"    {i}: ")
        if price == "q":
            return "q"
        if price == "s":
            return "s"
        if price is not None:
            entries.append({"price": price, "rating": i})
    return entries


def main():
    args = sys.argv[1:]
    ratings = load_ratings()
    skips = load_skips()

    # --watch TICKER PRICE : a 5-star level, the one you are waiting for
    if "--watch" in args:
        rest = args[args.index("--watch") + 1:]
        if not rest:
            print("  Specify a ticker: --watch AZO 2500")
            return
        ticker = rest[0].upper()
        entry = ratings.setdefault(ticker, {})
        buys = [e for e in entry.get("buys", []) if e["rating"] != 5]
        if len(rest) < 2:
            cur = next((e for e in entry.get("buys", []) if e["rating"] == 5), None)
            print("  %s watch: %s" % (ticker, "$%.2f" % cur["price"] if cur else "none set"))
            return
        price = float(rest[1].lstrip("$"))
        buys.append({"price": price, "rating": 5})
        entry["buys"] = sorted(buys, key=lambda e: e["rating"])
        entry["updated"] = date.today().isoformat()
        save_ratings(ratings)
        near = NEAR_PCT_BY_RATING.get(5, NEAR_PCT)
        print("  %s watching $%.2f  (5-star) - shows in WATCH CLOSING IN at $%.2f (%.1f%%)"
              % (ticker, price, price * (1 + near / 100), near))
        return

    if "--unwatch" in args:
        for t in [x.upper() for x in args[args.index("--unwatch") + 1:]]:
            e = ratings.get(t)
            if e and any(b["rating"] == 5 for b in e.get("buys", [])):
                e["buys"] = [b for b in e["buys"] if b["rating"] != 5]
                print("  %s watch cleared" % t)
            else:
                print("  %s had no watch" % t)
        save_ratings(ratings)
        return

    # --mutes / --mute / --unmute
    if "--mutes" in args:
        show_mutes(load_mutes())
        return

    if "--mute" in args:
        mutes = load_mutes()
        rest = args[args.index("--mute") + 1:]
        side = "sell" if "--sell" in rest else "buy"
        rest = [x for x in rest if x != "--sell"]
        days = MUTE_DAYS_DEFAULT
        if len(rest) > 2 and rest[-1].isdigit():
            days = int(rest[-1]); rest = rest[:-1]
        if not rest:
            print("  Specify a ticker: --mute AVGO [rung] [days]")
            return
        ticker = rest[0].upper()
        rung = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
        until = add_mute(mutes, ticker, rung, side, days)
        save_mutes(mutes)
        print("  Muted %s %s rung %s for %d days - back on %s"
              % (ticker, side, rung if rung else "ALL", days, until))
        return

    if "--unmute" in args:
        mutes = load_mutes()
        for t in [x.upper() for x in args[args.index("--unmute") + 1:]]:
            print("  %s un-muted" % t if mutes.pop(t, None) else "  %s was not muted" % t)
        save_mutes(mutes)
        return

    # --skips: list what is snoozed
    if "--skips" in args:
        show_skips(skips)
        return

    # --skip TICKER [days]
    if "--skip" in args:
        idx = args.index("--skip")
        rest = args[idx + 1:]
        days = SKIP_DAYS_DEFAULT
        if rest and rest[-1].isdigit():
            days = int(rest[-1])
            rest = rest[:-1]
        if not rest:
            print("  Specify tickers: --skip AVGO [days]")
            return
        for t in [x.upper() for x in rest]:
            until = add_skip(skips, t, days)
            print(f"  Skipping {t} for {days} days — back on {until}")
        save_skips(skips)
        return

    # --unskip TICKER
    if "--unskip" in args:
        idx = args.index("--unskip")
        for t in [x.upper() for x in args[idx + 1:]]:
            if skips.pop(t, None):
                print(f"  {t} is back in rotation")
            else:
                print(f"  {t} was not skipped")
        save_skips(skips)
        return

    # --show
    if "--show" in args:
        args.remove("--show")
        tickers = [t.upper() for t in args] if args else None
        show_ratings(ratings, tickers)
        return

    # --delete
    if "--delete" in args:
        idx = args.index("--delete")
        to_delete = [t.upper() for t in args[idx + 1:]]
        for t in to_delete:
            if t in ratings:
                del ratings[t]
                print(f"  Deleted {t}")
            else:
                print(f"  {t} not found")
        save_ratings(ratings)
        return

    # --edit mode: edit specific tickers (both buy + sell)
    if "--edit" in args:
        idx = args.index("--edit")
        to_edit = [t.upper() for t in args[idx + 1:]]
        if not to_edit:
            print("  Specify tickers: --edit AAPL MSFT")
            return
        for ticker in to_edit:
            existing = ratings.get(ticker, {})
            print(f"\n  ===  {ticker}  ===")
            if existing.get("buys") or existing.get("sells"):
                _show_one(ticker, existing)

            print(f"  BUY prices (Enter to skip, q to quit):")
            buys = rate_side(ticker, "buy")
            if buys == "q":
                save_ratings(ratings)
                return

            print(f"  SELL prices (Enter to skip, q to quit):")
            sells = rate_side(ticker, "sell")
            if sells == "q":
                save_ratings(ratings)
                return

            if buys or sells:
                result = {"updated": date.today().isoformat()}
                if buys:
                    result["buys"] = buys
                if sells:
                    result["sells"] = sells
                ratings[ticker] = result
                save_ratings(ratings)
                _print_saved(ticker, result)
            else:
                print(f"    Skipped {ticker}")
        return

    # Determine side: buy or sell
    side = "buy"
    if args and args[0].lower() in ("buy", "sell"):
        side = args.pop(0).lower()

    all_tickers = load_tickers()

    # --only
    if "--only" in args:
        idx = args.index("--only")
        only = [t.upper() for t in args[idx + 1:]]
        tickers = [t for t in all_tickers if t in only]
        if not tickers:
            print("  None of those tickers are in config.json")
            return
    else:
        tickers = all_tickers

    restart = "--restart" in args

    # --refresh
    refresh = "--refresh" in args
    refresh_days = 0
    if refresh:
        idx = args.index("--refresh")
        if idx + 1 < len(args) and args[idx + 1].isdigit():
            refresh_days = int(args[idx + 1])

    if refresh:
        rated_tickers = [t for t in tickers if t in ratings]
        if refresh_days > 0:
            rated_tickers = [
                t for t in rated_tickers
                if _days_since(ratings[t].get("updated", "")) >= refresh_days
            ]
        rated_tickers.sort(key=lambda t: ratings[t].get("updated", "0000-00-00"))
        remaining = rated_tickers
        mode = f"REFRESH {side.upper()} (oldest first, {len(remaining)} to review)"
    elif restart:
        remaining = tickers
        mode = f"RESTART {side.upper()} (all tickers)"
    else:
        rated = set(ratings.keys())
        remaining = [t for t in tickers if t not in rated]
        mode = f"NEW {side.upper()} (unrated tickers)"

    # Snoozed tickers drop out until their date passes
    snoozed = [t for t in remaining if t in skips]
    if snoozed:
        remaining = [t for t in remaining if t not in skips]
        nxt = min(skips[t]["until"] for t in snoozed)
        mode += f"  [{len(snoozed)} snoozed, next back {nxt}]"

    side_label = "BUY" if side == "buy" else "SELL"

    print()
    print("  ========================================")
    print(f"    STOCK RATING — {side_label}")
    print("  ========================================")
    print(f"  Mode: {mode}")
    print(f"  {len(remaining)} tickers to go.")
    print(f"  {len(ratings)} already rated out of {len(tickers)} total.")
    print()
    print(f"  Enter {side} prices for each stock.")
    print(f"  Entry 1 = rating 1, entry 2 = rating 2, entry 3 = rating 3.")
    print(f"  Enter to skip an entry. 3 blanks = next stock. s = snooze 2wk. q = quit.")
    print()

    count = 0
    for ticker in remaining:
        count += 1
        age_str = ""
        if ticker in ratings and ratings[ticker].get("updated"):
            age = _days_since(ratings[ticker]["updated"])
            age_str = f"  ({age}d ago)"

        existing = ratings.get(ticker, {})
        existing_entries = existing.get(f"{side}s", [])

        # Show existing if refreshing
        if existing_entries:
            cur = "  ".join(f"${e['price']:.2f}({e['rating']})" for e in existing_entries)
            print(f"  [{count}/{len(remaining)}]  {ticker}{age_str}  current: {cur}")
        else:
            print(f"  [{count}/{len(remaining)}]  {ticker}{age_str}")

        entries = rate_side(ticker, side)
        if entries == "q":
            save_ratings(ratings)
            print(f"\n  Saved. {len(ratings)} stocks rated.\n")
            return

        if entries == "s":
            until = add_skip(skips, ticker)
            save_skips(skips)
            print(f"    -> snoozed {SKIP_DAYS_DEFAULT}d, back on {until}")
            continue

        if entries:
            if ticker not in ratings:
                ratings[ticker] = {"updated": date.today().isoformat()}
            else:
                ratings[ticker]["updated"] = date.today().isoformat()
            ratings[ticker][f"{side}s"] = entries
            save_ratings(ratings)
            saved = "  ".join(f"${e['price']:.2f}({e['rating']})" for e in entries)
            print(f"    -> {saved}")

    print(f"\n  Done! {len(ratings)} stocks rated total.\n")
    save_ratings(ratings)


def _show_one(ticker: str, data: dict):
    """Show existing ratings inline."""
    buys = data.get("buys", [])
    sells = data.get("sells", [])
    if buys:
        cur = "  ".join(f"${e['price']:.2f}({e['rating']})" for e in buys)
        print(f"    Buys:  {cur}")
    if sells:
        cur = "  ".join(f"${e['price']:.2f}({e['rating']})" for e in sells)
        print(f"    Sells: {cur}")


def _print_saved(ticker: str, result: dict):
    print(f"    Saved {ticker}:")
    if result.get("buys"):
        cur = "  ".join(f"${e['price']:.2f}({e['rating']})" for e in result["buys"])
        print(f"      Buys:  {cur}")
    if result.get("sells"):
        cur = "  ".join(f"${e['price']:.2f}({e['rating']})" for e in result["sells"])
        print(f"      Sells: {cur}")


if __name__ == "__main__":
    main()
