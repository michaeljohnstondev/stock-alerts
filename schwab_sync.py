"""Sync your positions from Schwab - read only. No orders, ever.

Pulls the account's stock and ETF positions and compares them with
state/holdings.json: new names and adds are recorded as buys (snapshot,
rung filled, sell ladder - same as record_buy.py), names gone are sold
(their buy side shows on the report again), and quantities and cost basis
are set to Schwab's exact numbers.

Setup, once:
  1. In .env (never committed):
        SCHWAB_APP_KEY=...
        SCHWAB_APP_SECRET=...
        SCHWAB_CALLBACK_URL=https://127.0.0.1
        SCHWAB_ACCOUNT=256        # last digits of the account to sync
  2. In a terminal: python schwab_sync.py --login
     A Schwab page opens; log in, approve, then paste the address you land on
     (an error page at 127.0.0.1 is expected) back into the terminal.
     The login lasts ~7 days; run --login again when it expires.

Usage:
    python schwab_sync.py              # dry run: what changed, nothing written
    python schwab_sync.py --go         # record the changes
    python schwab_sync.py --baseline   # first sync: take Schwab's positions as
                                       #   the truth without recording buys
"""

import json
import os
import sys
from datetime import date

from dotenv import load_dotenv

from stock_checker.constants import ROOT_DIR, STATE_DIR
from stock_checker.holdings import HOLDINGS_FILE, load_holdings

load_dotenv(ROOT_DIR / ".env")
TOKEN_FILE = STATE_DIR / "schwab_token.json"   # gitignored with the rest of state/
STOCK_TYPES = ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def login():
    from schwab import auth
    auth.client_from_manual_flow(_env("SCHWAB_APP_KEY"), _env("SCHWAB_APP_SECRET"),
                                 _env("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
                                 str(TOKEN_FILE))
    print("\n  Logged in. Token saved; run: python schwab_sync.py")


def fetch_positions() -> tuple:
    """({ticker: (qty, avg_cost)}, [option lines], account label)"""
    from schwab import auth
    from schwab.client import Client
    c = auth.client_from_token_file(str(TOKEN_FILE), _env("SCHWAB_APP_KEY"),
                                    _env("SCHWAB_APP_SECRET"))
    r = c.get_accounts(fields=[Client.Account.Fields.POSITIONS])
    r.raise_for_status()
    want = _env("SCHWAB_ACCOUNT")
    accounts = [a["securitiesAccount"] for a in r.json()]
    picked = [a for a in accounts if not want or str(a.get("accountNumber", "")).endswith(want)]
    if len(picked) != 1:
        nums = ["*" + str(a.get("accountNumber", ""))[-3:] for a in accounts]
        raise SystemExit("  Set SCHWAB_ACCOUNT in .env to one of: %s" % ", ".join(nums))
    acct = picked[0]
    stocks, options = {}, []
    for p in acct.get("positions", []):
        inst = p.get("instrument", {})
        qty = p.get("longQuantity", 0) - p.get("shortQuantity", 0)
        if inst.get("assetType") in STOCK_TYPES and qty:
            stocks[inst["symbol"].replace("/", "-")] = (qty, round(p.get("averagePrice", 0), 2))
        elif inst.get("assetType") == "OPTION":
            options.append("%s  x%g" % (inst.get("symbol", "?"), qty))
    return stocks, options, "*" + str(acct.get("accountNumber", ""))[-3:]


def diff(mine: dict, theirs: dict) -> list:
    """[(kind, ticker, shares, price)] - kind: new, add, trim, sold."""
    out = []
    for t, (qty, avg) in sorted(theirs.items()):
        old_qty, old_avg = mine.get(t, (0, 0))
        if not old_qty:
            out.append(("new", t, qty, avg))
        elif qty > old_qty:
            # what the added shares cost, from the change in average cost
            added = qty - old_qty
            price = (qty * avg - old_qty * old_avg) / added
            out.append(("add", t, added, round(price, 2) if price > 0 else None))
        elif qty < old_qty:
            out.append(("trim", t, old_qty - qty, None))
    for t in sorted(set(mine) - set(theirs)):
        out.append(("sold", t, mine[t][0], None))
    return out


def recent_fills(days: int = 7) -> dict:
    """{(symbol, "BUY"|"SELL"): [(entered, qty, avg price)]} from filled orders."""
    from datetime import datetime, timedelta, timezone
    from schwab import auth
    c = auth.client_from_token_file(str(TOKEN_FILE), _env("SCHWAB_APP_KEY"),
                                    _env("SCHWAB_APP_SECRET"))
    want = _env("SCHWAB_ACCOUNT")
    acct = [a for a in c.get_account_numbers().json() if a["accountNumber"].endswith(want)][0]
    now = datetime.now(timezone.utc)
    r = c.get_orders_for_account(acct["hashValue"], from_entered_datetime=now - timedelta(days=days),
                                 to_entered_datetime=now + timedelta(hours=1))
    r.raise_for_status()
    out = {}
    for o in r.json():
        if o.get("status") != "FILLED":
            continue
        legs = [l for a in o.get("orderActivityCollection", []) for l in a.get("executionLegs", [])]
        qty = sum(l.get("quantity", 0) for l in legs) or o.get("filledQuantity", 0)
        price = (sum(l["quantity"] * l["price"] for l in legs) / qty) if legs and qty else o.get("price")
        for leg in o.get("orderLegCollection", []):
            ins = "BUY" if leg.get("instruction", "").startswith("BUY") else "SELL"
            out.setdefault((leg["instrument"]["symbol"], ins), []).append(
                (o.get("enteredTime", ""), qty, round(price, 2) if price else None))
    for v in out.values():
        v.sort(reverse=True)
    return out


def _fill_price(fills: dict, symbol: str, side: str, fallback):
    """The latest matching fill's price - more exact than the average-cost math."""
    hits = fills.get((symbol, side)) or []
    return hits[0][2] if hits and hits[0][2] else fallback


def sync(go: bool = True, baseline: bool = False, quiet: bool = False) -> list:
    """Bring holdings in line with Schwab. Buys and adds are recorded with a
    snapshot, sales and trims are filed in the ticker's history with P/L.
    Returns the changes."""
    say = (lambda *a: None) if quiet else print
    theirs, options, label = fetch_positions()
    holdings = load_holdings()
    # "manual": positions held outside this account - the sync leaves them be
    manual = holdings.get("manual", {})
    mine = {t: tuple(v) for t, v in holdings.get("positions", {}).items() if t not in manual}
    changes = diff(mine, theirs)

    say("  Schwab %s: %d stock/ETF positions%s" % (
        label, len(theirs), "" if go or baseline else "  (DRY RUN)"))
    for kind, t, n, price in changes:
        say("    %-5s %-6s %g sh%s" % (kind, t, n, " @ $%.2f" % price if price else ""))
    if not changes:
        say("    no changes")
    if options:
        say("  options (not tracked): " + "; ".join(options))

    if baseline:
        holdings["positions"] = {**{t: [q, a] for t, (q, a) in theirs.items()}, **manual}
    elif go:
        from record_buy import record, record_sell
        fills = recent_fills() if changes else {}
        for kind, t, n, price in changes:
            if kind in ("new", "add"):
                record(t, n, _fill_price(fills, t, "BUY", price),
                       note="taken at sync, not at the buy")
            else:  # sold or trimmed
                record_sell(t, n, _fill_price(fills, t, "SELL", None),
                            note="taken at sync, not at the sale", update_holdings=False)
        holdings = load_holdings()   # record() updated it
        holdings["positions"] = {**{t: [q, a] for t, (q, a) in theirs.items()}, **manual}
    else:
        return changes
    holdings["as_of"] = date.today().isoformat()
    holdings["account"] = "Schwab %s (synced)" % label
    HOLDINGS_FILE.write_text(json.dumps(holdings, indent=2), encoding="utf-8")
    say("  holdings set to Schwab's numbers - %d positions" % len(holdings["positions"]))
    return changes


def main():
    args = sys.argv[1:]
    if "--login" in args:
        return login()
    if not TOKEN_FILE.exists():
        raise SystemExit("  Not logged in yet. Run: python schwab_sync.py --login")
    sync(go="--go" in args, baseline="--baseline" in args)


if __name__ == "__main__":
    main()
