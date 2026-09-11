"""One-time (but re-runnable) import of the JSON state files into Postgres.

Nothing is deleted. The JSON files stay exactly where they are — this
only reads them. Run it as many times as you like: every statement is
an UPSERT, so a second run updates rather than duplicates.

    python db/import_json.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from connect import connect

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    """Read a JSON state file, or return None if it isn't there."""
    path = ROOT / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! {name} unreadable ({exc}) - skipping")
        return None


def num(v):
    """Coerce to float, turning NaN/inf/blank into NULL."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


FUND_COLS = [
    "trailing_pe", "forward_pe", "peg_ratio", "profit_margins",
    "operating_margins", "debt_to_equity", "return_on_equity",
    "revenue_growth", "earnings_growth", "free_cashflow",
    "market_cap", "short_pct_float", "beta", "target_mean_price",
]


def main() -> None:
    ratings = load(".user_ratings.json") or {}
    funds = load(".fundamental_cache.json") or {}
    sectors = load(".sector_cache.json") or {}
    alerts = load(".alert_state.json") or {}
    sector_st = load(".sector_state.json") or {}
    skipped = load(".skipped.json") or {}
    muted = load(".muted.json") or {}

    # ---- tickers: the union of every symbol mentioned anywhere ----
    symbols = set()
    for src in (ratings, funds, sectors, skipped, muted):
        symbols.update(k for k in src if isinstance(k, str))
    for kind in ("buy", "sell", "buy_near", "sell_near"):
        symbols.update((alerts.get(kind) or {}).keys())
    symbols = sorted(s for s in symbols if s and not s.startswith("_"))

    with connect() as conn:
        cur = conn.cursor()

        # Foreign keys mean tickers MUST exist before anything that
        # references them. Insert order is not optional here - that is
        # the constraint doing its job.
        cur.executemany(
            """INSERT INTO tickers (symbol, sector, industry)
               VALUES (%s, %s, %s)
               ON CONFLICT (symbol) DO UPDATE
                 SET sector     = COALESCE(EXCLUDED.sector,   tickers.sector),
                     industry   = COALESCE(EXCLUDED.industry, tickers.industry),
                     updated_at = now()""",
            [
                (s, (sectors.get(s) or {}).get("sector"),
                 (sectors.get(s) or {}).get("industry"))
                for s in symbols
            ],
        )
        print(f"  tickers            {len(symbols):>5}")

        # ---- rating_levels: flatten buys[] and sells[] into one table ----
        rows = []
        bad = 0
        for sym, data in ratings.items():
            if not isinstance(data, dict):
                continue
            for direction, list_key, src_key in (
                ("buy", "buys", "source"),
                ("sell", "sells", "sells_source"),
            ):
                source = data.get(src_key) or "manual"
                if source not in ("manual", "auto"):
                    source = "manual"
                for entry in data.get(list_key) or []:
                    if not isinstance(entry, dict):
                        bad += 1
                        continue
                    price = num(entry.get("price"))
                    rung = entry.get("rating")
                    # The CHECK constraints would reject these anyway.
                    # Filtering here just produces a clearer report.
                    if not price or price <= 0:
                        bad += 1
                        continue
                    if not isinstance(rung, int) or not 1 <= rung <= 8:
                        bad += 1
                        continue
                    rows.append((sym, direction, rung, price, source))

        cur.executemany(
            """INSERT INTO rating_levels (symbol, direction, rung, price, source)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (symbol, direction, rung) DO UPDATE
                 SET price      = EXCLUDED.price,
                     source     = EXCLUDED.source,
                     updated_at = now()""",
            rows,
        )
        note = f"   ({bad} rejected)" if bad else ""
        print(f"  rating_levels      {len(rows):>5}{note}")

        # ---- fundamentals: typed columns + JSONB for everything else ----
        rows = []
        for sym, d in funds.items():
            if not isinstance(d, dict):
                continue
            ts = d.get("_timestamp")
            if isinstance(ts, (int, float)):
                fetched = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                fetched = datetime.now(timezone.utc)
            known = set(FUND_COLS) | {"_timestamp", "analyst_count", "recommendation"}
            extra = {k: v for k, v in d.items() if k not in known}
            ac = d.get("analyst_count")
            rows.append((
                sym, fetched,
                *[num(d.get(c)) for c in FUND_COLS],
                int(ac) if isinstance(ac, (int, float)) else None,
                d.get("recommendation"),
                json.dumps(extra),
            ))

        placeholders = ", ".join(["%s"] * (len(FUND_COLS) + 5))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in FUND_COLS)
        cur.executemany(
            f"""INSERT INTO fundamentals
                  (symbol, fetched_at, {', '.join(FUND_COLS)},
                   analyst_count, recommendation, raw)
                VALUES ({placeholders})
                ON CONFLICT (symbol) DO UPDATE SET
                  fetched_at     = EXCLUDED.fetched_at,
                  {updates},
                  analyst_count  = EXCLUDED.analyst_count,
                  recommendation = EXCLUDED.recommendation,
                  raw            = EXCLUDED.raw""",
            rows,
        )
        print(f"  fundamentals       {len(rows):>5}")

        # ---- alert_state: four nested dicts collapse into one table ----
        rows = [
            (sym, kind, num(level))
            for kind in ("buy", "sell", "buy_near", "sell_near")
            for sym, level in (alerts.get(kind) or {}).items()
            if num(level) is not None
        ]
        cur.executemany(
            """INSERT INTO alert_state (symbol, kind, level)
               VALUES (%s, %s, %s)
               ON CONFLICT (symbol, kind) DO UPDATE
                 SET level = EXCLUDED.level, updated_at = now()""",
            rows,
        )
        print(f"  alert_state        {len(rows):>5}")

        # ---- sector_snapshots: today's file becomes one dated snapshot ----
        rows = []
        if isinstance(sector_st.get("sectors"), dict):
            ts = sector_st.get("timestamp")
            captured = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            spy = num(sector_st.get("spy_close"))
            rows = [
                (captured, name, d.get("rank"), num(d.get("composite")),
                 d.get("status"), num(d.get("close")), spy)
                for name, d in sector_st["sectors"].items()
                if isinstance(d, dict)
            ]
            cur.executemany(
                """INSERT INTO sector_snapshots
                     (captured_at, sector, rank, composite, status, close, spy_close)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (captured_at, sector) DO UPDATE
                     SET rank      = EXCLUDED.rank,
                         composite = EXCLUDED.composite,
                         status    = EXCLUDED.status,
                         close     = EXCLUDED.close""",
                rows,
            )
        print(f"  sector_snapshots   {len(rows):>5}")

        # ---- suppressions: .skipped.json + .muted.json share one shape ----
        rows = []
        for kind, src in (("skipped", skipped), ("muted", muted)):
            for sym, d in (src or {}).items():
                if not isinstance(d, dict):
                    continue
                rows.append((sym, kind, d.get("since"), d.get("until"), d.get("reason")))
        cur.executemany(
            """INSERT INTO suppressions (symbol, kind, since, until, reason)
               VALUES (%s, %s, COALESCE(%s::date, CURRENT_DATE), %s::date, %s)
               ON CONFLICT (symbol, kind) DO UPDATE
                 SET until = EXCLUDED.until, reason = EXCLUDED.reason""",
            rows,
        )
        print(f"  suppressions       {len(rows):>5}")

        # One commit for the whole import. Any failure above rolls the
        # entire thing back - the database is never left half-loaded.
        conn.commit()
        print("\nCommitted.")


if __name__ == "__main__":
    main()
