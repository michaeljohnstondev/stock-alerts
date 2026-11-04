"""Place stock/ETF limit orders on Schwab - two steps, with your yes between.

    1. python trade.py preview buy SCCO 2 173.00
       Checks every rule below, asks Schwab to preview that exact order, and
       saves it as pending with a short code. Nothing is sent.
    2. You read the preview and say yes. Then:
       python trade.py send <code>
       Sends only that pending order, only within 5 minutes of the preview.

       Add a session to trade outside regular hours (limit orders only, as always):
         --session ext   pre-market + regular + after-hours (Schwab "seamless")
         --session pm    after-hours only (about 4-8 PM ET)
         --session am    pre-market only (about 7-9:30 AM ET)
       Orders are good-till-cancel with extended hours by default (your call:
       always GTC). Add --day for a day order, --session normal for regular
       hours only.

    python trade.py orders              # today's orders on the account
    python trade.py cancel <order id>   # cancel a working order

Rules the code enforces, whatever it is asked to do:
  - TRADING_ENABLED=true in .env, or nothing is sent (the kill switch)
  - limit orders only; stocks and ETFs only; the SCHWAB_ACCOUNT account only
  - buys: at most MAX_ORDER_USD per order and MAX_DAY_USD per day
  - sells: never more shares than you hold (no shorting by accident)
  - the limit must be within 10% of the live quote (fat-finger guard)
Every preview, send and cancel is appended to state/orders.log.
"""

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

from stock_checker.constants import STATE_DIR
from schwab_sync import TOKEN_FILE, _env

PENDING_FILE = STATE_DIR / "pending_order.json"
LOG_FILE = STATE_DIR / "orders.log"
PENDING_TTL = timedelta(minutes=5)
FAT_FINGER = 0.10
TRADABLE = ("EQUITY", "ETF", "COLLECTIVE_INVESTMENT")


def _limits() -> tuple:
    return (float(_env("MAX_ORDER_USD", "500")), float(_env("MAX_DAY_USD", "5000")))


def _client():
    from schwab import auth
    if not TOKEN_FILE.exists():
        raise SystemExit("  Not logged in. Run: python schwab_sync.py --login")
    return auth.client_from_token_file(str(TOKEN_FILE), _env("SCHWAB_APP_KEY"),
                                       _env("SCHWAB_APP_SECRET"))


def _account_hash(c) -> str:
    want = _env("SCHWAB_ACCOUNT")
    hits = [a for a in c.get_account_numbers().json() if a["accountNumber"].endswith(want)]
    if not want or len(hits) != 1:
        raise SystemExit("  SCHWAB_ACCOUNT in .env must match exactly one account")
    return hits[0]["hashValue"]


def _log(event: str, data: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                            "event": event, **data}) + "\n")


def _bought_today() -> float:
    if not LOG_FILE.exists():
        return 0.0
    today = datetime.now().date().isoformat()
    total = 0.0
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["event"] == "sent" and e["at"].startswith(today) and e["side"] == "buy":
            total += e["qty"] * e["limit"]
    return total


def _held_shares(c, acct_hash: str, symbol: str) -> float:
    from schwab.client import Client
    r = c.get_account(acct_hash, fields=[Client.Account.Fields.POSITIONS])
    r.raise_for_status()
    for p in r.json()["securitiesAccount"].get("positions", []):
        if p.get("instrument", {}).get("symbol") == symbol:
            return p.get("longQuantity", 0)
    return 0


def _check(c, acct_hash: str, side: str, symbol: str, qty: float, limit: float) -> list:
    """Every rule, in one place. Returns the reasons it fails (empty = ok)."""
    problems = []
    if side not in ("buy", "sell"):
        problems.append("side must be buy or sell")
    if qty <= 0 or qty != int(qty):
        problems.append("quantity must be a whole number of shares")
    if limit <= 0:
        problems.append("limit price must be positive")
    try:
        q = c.get_quote(symbol).json().get(symbol)
    except Exception:
        q = None
    if not q:
        return problems + [f"no quote for {symbol}"]
    if q.get("assetMainType") not in TRADABLE:
        problems.append(f"{symbol} is {q.get('assetMainType')} - stocks and ETFs only")
    last = (q.get("quote") or {}).get("lastPrice") or 0
    if last and abs(limit / last - 1) > FAT_FINGER:
        problems.append(f"limit ${limit:.2f} is more than 10% from the last price ${last:.2f}")
    per_order, per_day = _limits()
    cost = qty * limit
    if side == "buy":
        if cost > per_order:
            problems.append(f"${cost:,.2f} is over the ${per_order:,.0f} per-order cap")
        spent = _bought_today()
        if spent + cost > per_day:
            problems.append(f"would bring today to ${spent + cost:,.2f}, over the ${per_day:,.0f} daily cap")
    else:
        held = _held_shares(c, acct_hash, symbol)
        if qty > held:
            problems.append(f"you hold {held:g} {symbol}, can't sell {qty:g}")
    return problems


SESSIONS = {"normal": "NORMAL", "ext": "SEAMLESS", "pm": "PM", "am": "AM"}
SESSION_LABEL = {"normal": "regular hours", "ext": "extended hours (pre + regular + after)",
                 "pm": "after-hours only", "am": "pre-market only"}


def preview(side: str, symbol: str, qty: float, limit: float, session: str = "normal",
            gtc: bool = False):
    from schwab.orders.equities import equity_buy_limit, equity_sell_limit
    c = _client()
    acct = _account_hash(c)
    symbol = symbol.upper()
    problems = _check(c, acct, side, symbol, qty, limit)
    if problems:
        print("  REFUSED:\n    - " + "\n    - ".join(problems))
        _log("refused", {"side": side, "symbol": symbol, "qty": qty, "limit": limit,
                         "why": problems})
        return
    build = equity_buy_limit if side == "buy" else equity_sell_limit
    if session not in SESSIONS:
        raise SystemExit("  --session must be one of: " + ", ".join(SESSIONS))
    from schwab.orders.common import Session
    order = build(symbol, int(qty), f"{limit:.2f}")
    order.set_session(Session[SESSIONS[session]])
    if gtc:
        from schwab.orders.common import Duration
        order.set_duration(Duration.GOOD_TILL_CANCEL)
    spec = order.build()
    pr = c.preview_order(acct, spec)
    body = pr.json() if pr.content else {}
    warnings = [m.get("message") for m in
                ((body.get("orderValidationResult") or {}).get("warns") or []) +
                ((body.get("orderValidationResult") or {}).get("rejects") or [])]
    rejects = (body.get("orderValidationResult") or {}).get("rejects")
    if pr.status_code >= 400 or rejects:
        print(f"  Schwab rejected the preview ({pr.status_code}): {warnings or pr.text[:300]}")
        return
    code = secrets.token_hex(3)
    pending = {"code": code, "side": side, "symbol": symbol, "qty": qty, "limit": limit,
               "session": session, "gtc": gtc,
               "spec": spec, "created": datetime.now(timezone.utc).isoformat()}
    PENDING_FILE.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    _log("preview", {k: pending[k] for k in ("code", "side", "symbol", "qty", "limit")})
    print(f"  PREVIEW  {side.upper()} {qty:g} {symbol} LIMIT ${limit:.2f}  "
          f"({'good-till-cancel' if gtc else 'day order'}, {SESSION_LABEL[session]})")
    print(f"           total about ${qty * limit:,.2f}")
    for w in filter(None, warnings):  # Schwab sends some warnings with no text
        print(f"           Schwab note: {w}")
    print(f"  Not sent. To send: python trade.py send {code}   (valid 5 minutes)")


def send(code: str):
    if _env("TRADING_ENABLED").lower() != "true":
        raise SystemExit("  Trading is off (TRADING_ENABLED is not true in .env). Nothing sent.")
    if not PENDING_FILE.exists():
        raise SystemExit("  No pending order. Preview one first.")
    p = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    if p["code"] != code:
        raise SystemExit("  Code doesn't match the pending order. Nothing sent.")
    if datetime.now(timezone.utc) - datetime.fromisoformat(p["created"]) > PENDING_TTL:
        PENDING_FILE.unlink()
        raise SystemExit("  That preview expired (5 minutes). Preview again.")
    c = _client()
    acct = _account_hash(c)
    # the rules again at send time - prices and the day's total may have moved
    problems = _check(c, acct, p["side"], p["symbol"], p["qty"], p["limit"])
    if problems:
        PENDING_FILE.unlink()
        raise SystemExit("  REFUSED at send:\n    - " + "\n    - ".join(problems))
    r = c.place_order(acct, p["spec"])
    PENDING_FILE.unlink()
    order_id = r.headers.get("location", "").rstrip("/").split("/")[-1]
    ok = r.status_code in (200, 201)
    _log("sent" if ok else "send_failed", {"side": p["side"], "symbol": p["symbol"],
                                          "qty": p["qty"], "limit": p["limit"],
                                          "order_id": order_id, "status": r.status_code})
    if ok:
        print(f"  SENT  {p['side'].upper()} {p['qty']:g} {p['symbol']} @ ${p['limit']:.2f}  order {order_id}")
        _record_if_filled(c, acct, order_id, p)
    else:
        print(f"  Schwab did not accept it ({r.status_code}): {r.text[:300]}")


def _record_if_filled(c, acct: str, order_id: str, p: dict, wait_s: int = 15):
    """Give the order a few seconds; if it fills, record it at the real fill
    price (snapshot, ladder, holdings, sells) and refresh the page. If not,
    the sync at the start of the next scan picks it up."""
    import subprocess
    import time
    o = {}
    for _ in range(wait_s // 3):
        time.sleep(3)
        o = c.get_order(order_id, acct).json()
        if o.get("status") in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
    if o.get("status") != "FILLED":
        print(f"  status {o.get('status', '?')} - it will be recorded by the sync at the next scan")
        return
    legs = [l for a in o.get("orderActivityCollection", []) for l in a.get("executionLegs", [])]
    qty = sum(l["quantity"] for l in legs) or p["qty"]
    price = sum(l["quantity"] * l["price"] for l in legs) / qty if legs else p["limit"]
    print(f"  FILLED {qty:g} @ ${price:.2f}")
    from record_buy import record, record_sell
    if p["side"] == "buy":
        record(p["symbol"], qty, price)
    else:
        record_sell(p["symbol"], qty, price)
    subprocess.run([sys.executable, "-m", "stock_checker.html_report"], capture_output=True)
    print("  recorded, and the page is refreshed")


def orders():
    c = _client()
    acct = _account_hash(c)
    now = datetime.now(timezone.utc)
    r = c.get_orders_for_account(acct, from_entered_datetime=now - timedelta(days=1),
                                 to_entered_datetime=now + timedelta(hours=1))
    r.raise_for_status()
    for o in r.json():
        for leg in o.get("orderLegCollection", []):
            print("  %-12s %-6s %-5s %4g  limit %-8s %-10s filled %g" % (
                o.get("orderId"), leg["instrument"].get("symbol"), leg.get("instruction"),
                leg.get("quantity", 0), o.get("price", "-"), o.get("status"), o.get("filledQuantity", 0)))


def cancel(order_id: str):
    c = _client()
    r = c.cancel_order(order_id, _account_hash(c))
    _log("cancel", {"order_id": order_id, "status": r.status_code})
    print("  cancelled" if r.status_code in (200, 201) else f"  cancel failed ({r.status_code}): {r.text[:200]}")


def main():
    a = sys.argv[1:]
    session = "ext"           # default: pre-market + regular + after-hours
    gtc = "--day" not in a    # default: good-till-cancel
    for flag in ("--day", "--gtc"):
        if flag in a:
            a.remove(flag)
    if "--session" in a:
        i = a.index("--session")
        session = a[i + 1].lower()
        del a[i:i + 2]
    if len(a) == 5 and a[0] == "preview":
        return preview(a[1].lower(), a[2], float(a[3]), float(a[4]), session, gtc)
    if len(a) == 2 and a[0] == "send":
        return send(a[1])
    if a == ["orders"]:
        return orders()
    if len(a) == 2 and a[0] == "cancel":
        return cancel(a[1])
    print(__doc__)


if __name__ == "__main__":
    main()
