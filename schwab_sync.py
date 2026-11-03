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


def main():
    args = sys.argv[1:]
    if "--login" in args:
        return login()
    if not TOKEN_FILE.exists():
        raise SystemExit("  Not logged in yet. Run: python schwab_sync.py --login")
    go, baseline = "--go" in args, "--baseline" in args

    theirs, options, label = fetch_positions()
    holdings = load_holdings()
    # "manual": positions held outside this account - the sync leaves them be
    manual = holdings.get("manual", {})
    mine = {t: tuple(v) for t, v in holdings.get("positions", {}).items() if t not in manual}
    changes = diff(mine, theirs)

    print("  Schwab %s: %d stock/ETF positions%s" % (
        label, len(theirs), "" if go or baseline else "  (DRY RUN)"))
    for kind, t, n, price in changes:
        print("    %-5s %-6s %g sh%s" % (kind, t, n, " @ $%.2f" % price if price else ""))
    if not changes:
        print("    no changes")
    if options:
        print("  options (not tracked): " + "; ".join(options))

    if baseline:
        holdings["positions"] = {**{t: [q, a] for t, (q, a) in theirs.items()}, **manual}
    elif go:
        from record_buy import record
        for kind, t, n, price in changes:
            if kind in ("new", "add"):
                record(t, n, price, note="taken at sync, not at the buy")
        holdings = load_holdings()   # record() updated it
        holdings["positions"] = {**{t: [q, a] for t, (q, a) in theirs.items()}, **manual}
    else:
        return
    holdings["as_of"] = date.today().isoformat()
    holdings["account"] = "Schwab %s (synced)" % label
    HOLDINGS_FILE.write_text(json.dumps(holdings, indent=2), encoding="utf-8")
    print("  holdings set to Schwab's numbers - %d positions" % len(holdings["positions"]))


if __name__ == "__main__":
    main()
