"""Response schemas.

These are not decoration. FastAPI uses them to validate what the
handlers return, to coerce types (Postgres NUMERIC arrives as Decimal,
not float), and to generate the OpenAPI spec that powers /docs and,
later, typed clients for the dashboard.

A field the query stops returning becomes a startup-time error here
instead of an undefined in the front end.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Run(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    tickers_scanned: int | None = None
    api_calls: int | None = None
    market_trends: dict = {}
    report_path: str | None = None


class Alert(BaseModel):
    symbol: str
    sector: str | None = None
    direction: str
    rung: int
    target_price: float
    price: float
    pct_away: float
    triggered: bool
    escalated: bool
    prev_rung: int | None = None
    observed_at: datetime
    grade: str | None = None
    value_score: int | None = None


class Rung(BaseModel):
    direction: str
    rung: int
    price: float
    source: str
    pct_away: float | None = None
    triggered: bool | None = None


class PricePoint(BaseModel):
    observed_at: datetime
    price: float


class TickerDetail(BaseModel):
    symbol: str
    sector: str | None = None
    industry: str | None = None
    price: float | None = None
    observed_at: datetime | None = None
    grade: str | None = None
    value_score: int | None = None
    valuation: int | None = None
    profitability: int | None = None
    health: int | None = None
    growth: int | None = None
    forward_pe: float | None = None
    market_cap: int | None = None
    ladder: list[Rung] = []
    price_history: list[PricePoint] = []


class GradeBucket(BaseModel):
    grade: str
    names: int
    avg_score: float | None = None


class SectorRow(BaseModel):
    sector: str
    rank: int
    composite: float
    status: str
    captured_at: datetime
    rank_change: int | None = None


class Stats(BaseModel):
    tickers: int
    rating_levels: int
    runs: int
    alert_events: int
    price_observations: int
    latest_run_at: datetime | None = None
    history_days: int = 0
