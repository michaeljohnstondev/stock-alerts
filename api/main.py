"""Read-only HTTP API over the stock-alerts Postgres data.

    uvicorn api.main:app --reload

Then open http://127.0.0.1:8000/docs for the generated, interactive docs.

Read-only on purpose: the scanner owns writes. An API that can only
SELECT cannot corrupt the history it exists to show.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .db import close_pool, fetch_all, fetch_one, open_pool
from .models import (Alert, GradeBucket, Run, SectorRow, Stats, TickerDetail)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Open the pool once at startup, not on the first request, so the
    # first caller doesn't eat the connection handshake.
    open_pool()
    yield
    close_pool()


app = FastAPI(
    title="stock-alerts API",
    description="Rating ladders, alert history, and value grades.",
    version="0.1.0",
    lifespan=lifespan,
)

# The Next.js dashboard will run on a different port in development,
# which the browser treats as a different origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Every query below reads from the most recent run. Defining "latest"
# once keeps the endpoints honest about looking at the same snapshot.
LATEST_RUN = "(SELECT max(observed_at) FROM alert_events)"


@app.get("/health")
def health() -> dict:
    row = fetch_one("SELECT 1 AS ok")
    return {"status": "ok" if row else "degraded"}


@app.get("/stats", response_model=Stats)
def stats() -> dict:
    return fetch_one("""
        SELECT (SELECT count(*) FROM tickers)            AS tickers,
               (SELECT count(*) FROM rating_levels)      AS rating_levels,
               (SELECT count(*) FROM scan_runs)          AS runs,
               (SELECT count(*) FROM alert_events)       AS alert_events,
               (SELECT count(*) FROM price_observations) AS price_observations,
               (SELECT max(started_at) FROM scan_runs)   AS latest_run_at,
               COALESCE((SELECT (max(observed_at)::date - min(observed_at)::date)
                         FROM price_observations), 0)    AS history_days
    """)


@app.get("/runs", response_model=list[Run])
def runs(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    return fetch_all("""
        SELECT id, started_at, finished_at,
               EXTRACT(EPOCH FROM (finished_at - started_at)) AS duration_seconds,
               tickers_scanned, api_calls, market_trends, report_path
        FROM scan_runs
        ORDER BY started_at DESC
        LIMIT %s
    """, (limit,))


@app.get("/alerts", response_model=list[Alert])
def alerts(
    direction: str | None = Query(None, pattern="^(buy|sell)$"),
    min_rung: int = Query(1, ge=1, le=8),
    min_score: int | None = Query(None, ge=0, le=100),
    sector: str | None = None,
    triggered_only: bool = True,
    escalated_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    """Alerts from the most recent run.

    Filters are appended as parameterised fragments. The SQL text is
    built from fixed strings only -- user input never reaches it, it
    only ever arrives as a bound parameter.
    """
    where = [f"a.observed_at = {LATEST_RUN}", "a.rung >= %s"]
    params: list = [min_rung]

    if triggered_only:
        where.append("a.triggered = TRUE")
    if escalated_only:
        where.append("a.escalated = TRUE")
    if direction:
        where.append("a.direction = %s")
        params.append(direction)
    if sector:
        where.append("t.sector = %s")
        params.append(sector)
    if min_score is not None:
        where.append("v.score >= %s")
        params.append(min_score)

    params.append(limit)
    return fetch_all(f"""
        SELECT a.symbol, t.sector, a.direction, a.rung, a.target_price,
               a.price, a.pct_away, a.triggered, a.escalated, a.prev_rung,
               a.observed_at, v.grade, v.score AS value_score
        FROM alert_events a
        JOIN tickers t ON t.symbol = a.symbol
        LEFT JOIN value_scores v
               ON v.symbol = a.symbol AND v.observed_at = a.observed_at
        WHERE {' AND '.join(where)}
        ORDER BY a.rung DESC, a.pct_away ASC
        LIMIT %s
    """, tuple(params))


@app.get("/tickers/{symbol}", response_model=TickerDetail)
def ticker_detail(symbol: str, history: int = Query(60, ge=1, le=500)) -> dict:
    symbol = symbol.upper()

    base = fetch_one("""
        SELECT t.symbol, t.sector, t.industry,
               f.forward_pe, f.market_cap
        FROM tickers t
        LEFT JOIN fundamentals f ON f.symbol = t.symbol
        WHERE t.symbol = %s
    """, (symbol,))
    if not base:
        raise HTTPException(status_code=404, detail=f"{symbol} not found")

    # DISTINCT ON is a Postgres speciality: one row per group, picking
    # the first after ORDER BY. The portable alternative is a window
    # function or a correlated subquery; this is shorter and faster.
    latest = fetch_one("""
        SELECT DISTINCT ON (symbol) observed_at, price
        FROM price_observations WHERE symbol = %s
        ORDER BY symbol, observed_at DESC
    """, (symbol,))

    value = fetch_one("""
        SELECT DISTINCT ON (symbol) grade, score AS value_score,
               valuation, profitability, health, growth
        FROM value_scores WHERE symbol = %s
        ORDER BY symbol, observed_at DESC
    """, (symbol,))

    ladder = fetch_all("""
        SELECT r.direction, r.rung, r.price, r.source,
               a.pct_away, a.triggered
        FROM rating_levels r
        LEFT JOIN alert_events a
               ON a.symbol = r.symbol AND a.direction = r.direction
              AND a.rung = r.rung AND a.observed_at = %s
        WHERE r.symbol = %s
        ORDER BY r.direction, r.rung
    """, (latest["observed_at"] if latest else None, symbol))

    price_history = fetch_all("""
        SELECT observed_at, price FROM price_observations
        WHERE symbol = %s ORDER BY observed_at DESC LIMIT %s
    """, (symbol, history))

    return {
        **base,
        **(value or {}),
        "price": latest["price"] if latest else None,
        "observed_at": latest["observed_at"] if latest else None,
        "ladder": ladder,
        "price_history": list(reversed(price_history)),
    }


@app.get("/grades", response_model=list[GradeBucket])
def grades() -> list[dict]:
    """Grade distribution across the latest run."""
    return fetch_all("""
        SELECT grade, count(*) AS names, round(avg(score), 1) AS avg_score
        FROM value_scores
        WHERE observed_at = (SELECT max(observed_at) FROM value_scores)
        GROUP BY grade
        ORDER BY avg_score DESC NULLS LAST
    """)


@app.get("/sectors", response_model=list[SectorRow])
def sectors() -> list[dict]:
    """Latest sector rotation, with movement since the previous snapshot.

    LAG() is a window function: it reaches back to the previous row
    within each sector's own timeline. Comparing a row to its own
    history is the thing plain GROUP BY cannot do.
    """
    return fetch_all("""
        WITH ranked AS (
            SELECT sector, rank, composite, status, captured_at,
                   LAG(rank) OVER (PARTITION BY sector ORDER BY captured_at)
                       AS prev_rank
            FROM sector_snapshots
        )
        SELECT sector, rank, composite, status, captured_at,
               (prev_rank - rank) AS rank_change
        FROM ranked
        WHERE captured_at = (SELECT max(captured_at) FROM sector_snapshots)
        ORDER BY rank
    """)


@app.get("/symbols")
def symbols() -> list[str]:
    """Every symbol that has a rating ladder — for dashboard autocomplete."""
    rows = fetch_all(
        "SELECT DISTINCT symbol FROM rating_levels ORDER BY symbol")
    return [r["symbol"] for r in rows]
