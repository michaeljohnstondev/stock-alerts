"""Persist a ratings run to Postgres.

Additive and fail-soft by design:

  * The JSON state files are still written exactly as before. This
    module only ever INSERTs; it never reads state back into the run.
  * If DATABASE_URL is unset, this is a silent no-op.
  * If the database is unreachable or a write fails, it prints one
    warning and returns. The report still runs. A dashboard being
    down must never break the tool you actually use.

What it captures that the JSON files discard:

  scan_runs           one row per run
  price_observations  the live price already fetched, per ticker
  alert_events        EVERY rung evaluated -- not just the ones that
                      made the report -- so the record is complete
  value_scores        the grade score_value() computes and drops
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

try:
    from .connect import connect
except ImportError:  # running as a plain script
    from connect import connect


def _enabled() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def record_run(
    *,
    evaluations: list,
    ticker_data: dict,
    report_lists: dict,
    sector_rotation: dict | None = None,
    spy_close: float | None = None,
    market_trends=None,
    report_path=None,
    started_at: datetime | None = None,
    api_calls: int | None = None,
) -> None:
    """Write one run to Postgres. Never raises.

    evaluations: [(ticker, side, info, price)] for every rung checked,
                 where info is the _classify_entry dict.
    report_lists: {"buy": [...], "sell": [...], "buy_near": [...],
                   "sell_near": [...]} -- the post-filter report lists,
                 used only to overlay the escalated/prev_rung flags.
    """
    if not _enabled():
        return

    try:
        _record(evaluations, ticker_data, report_lists, sector_rotation,
                spy_close, market_trends, report_path, started_at, api_calls)
    except Exception as exc:
        # One line, no traceback, no re-raise. The report is the product.
        print(f"  [db] run not recorded: {type(exc).__name__}: {exc}")


def _record(evaluations, ticker_data, report_lists, sector_rotation,
            spy_close, market_trends, report_path, started_at, api_calls):
    observed_at = datetime.now(timezone.utc)

    # Escalation flags live on the filtered report lists. Key them so the
    # full evaluation set can pick them up. A rung that was filtered out
    # (muted, not deepest) simply has no flag -- which is correct: it was
    # not escalated, because it was not reported.
    flags: dict[tuple, tuple] = {}
    for key, items in (report_lists or {}).items():
        side = "buy" if key.startswith("buy") else "sell"
        for it in items or []:
            flags[(it.get("ticker"), side, it.get("rating"))] = (
                bool(it.get("escalated")), it.get("prev_rung"))

    # get_market_trend() returns one verdict per benchmark, e.g.
    # {"SPY": "bullish", "QQQ": "bullish"}. Stored whole as JSONB.
    trends = market_trends if isinstance(market_trends, dict) else {}

    with connect() as conn:
        cur = conn.cursor()

        # ---- scan_runs: the parent row everything else belongs to ----
        run_id = cur.execute(
            """INSERT INTO scan_runs (started_at, finished_at, tickers_scanned,
                                      api_calls, market_trends, report_path)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (started_at or observed_at, observed_at, len(ticker_data or {}),
             api_calls, __import__("json").dumps(trends),
             str(report_path) if report_path else None),
        ).fetchone()[0]

        # ---- tickers: a rating may name a symbol the DB hasn't seen ----
        symbols = sorted({t for t in (ticker_data or {}) if t})
        cur.executemany(
            "INSERT INTO tickers (symbol) VALUES (%s) ON CONFLICT DO NOTHING",
            [(s,) for s in symbols],
        )

        # ---- price_observations: the number every run throws away ----
        prices = [
            (sym, observed_at, _num((td or {}).get("price")), run_id)
            for sym, td in (ticker_data or {}).items()
        ]
        prices = [p for p in prices if p[2] and p[2] > 0]
        cur.executemany(
            """INSERT INTO price_observations (symbol, observed_at, price, run_id)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (symbol, observed_at) DO UPDATE
                 SET price = EXCLUDED.price""",
            prices,
        )

        # ---- alert_events: every rung evaluated, not just reported ----
        rows = []
        for ticker, side, info, price in evaluations or []:
            target = _num(info.get("target"))
            px = _num(price)
            rung = info.get("rating")
            if not target or target <= 0 or not px or px <= 0:
                continue
            if not isinstance(rung, int) or not 1 <= rung <= 8:
                continue
            escalated, prev_rung = flags.get((ticker, side, rung), (False, None))
            rows.append((
                ticker, observed_at, side, rung, target, px,
                _num(info.get("pct_away")), bool(info.get("triggered")),
                escalated, prev_rung, run_id,
            ))

        cur.executemany(
            """INSERT INTO alert_events (symbol, observed_at, direction, rung,
                                         target_price, price, pct_away,
                                         triggered, escalated, prev_rung, run_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol, direction, rung, observed_at) DO UPDATE
                 SET price     = EXCLUDED.price,
                     pct_away  = EXCLUDED.pct_away,
                     triggered = EXCLUDED.triggered,
                     escalated = EXCLUDED.escalated""",
            rows,
        )

        # ---- value_scores: the grade, finally written down ----
        vrows = []
        for sym, td in (ticker_data or {}).items():
            v = (td or {}).get("value")
            if not isinstance(v, dict) or v.get("score") is None:
                continue
            import json as _json
            vrows.append((
                sym, observed_at, "bullish", v.get("grade"), v.get("score"),
                v.get("valuation"), v.get("profitability"),
                v.get("health"), v.get("growth"),
                _json.dumps(v.get("details") or {}), run_id,
            ))
        cur.executemany(
            """INSERT INTO value_scores (symbol, observed_at, direction, grade,
                                         score, valuation, profitability,
                                         health, growth, details, run_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol, observed_at, direction) DO UPDATE
                 SET grade = EXCLUDED.grade, score = EXCLUDED.score,
                     details = EXCLUDED.details""",
            vrows,
        )

        # ---- sector_snapshots: accumulate instead of overwrite ----
        srows = []
        if isinstance(sector_rotation, dict):
            for name, d in sector_rotation.items():
                if not isinstance(d, dict) or d.get("rank") is None:
                    continue
                srows.append((observed_at, name, d.get("rank"),
                              _num(d.get("composite")), d.get("status"),
                              _num(d.get("close")), _num(spy_close)))
            cur.executemany(
                """INSERT INTO sector_snapshots (captured_at, sector, rank,
                                                 composite, status, close, spy_close)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (captured_at, sector) DO UPDATE
                     SET rank = EXCLUDED.rank, composite = EXCLUDED.composite""",
                srows,
            )

        conn.commit()

    print(f"  [db] run {run_id}: {len(prices)} prices, {len(rows)} alert rows, "
          f"{len(vrows)} value scores, {len(srows)} sectors")
