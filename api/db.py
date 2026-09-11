"""Connection pooling for the API.

A script like db/q.py can open a connection, run one query, and close it.
An API cannot: opening a Postgres connection costs ~50-100ms of TCP and
TLS handshake, and paying that per request would dominate response time.
A burst of traffic would also open a burst of connections, and Neon's
free tier caps them.

A pool opens a small set of connections once and hands them out per
request, so the handshake cost is paid at startup instead.

Sync pool, not async, on purpose. psycopg's async mode cannot run on
Windows' default ProactorEventLoop, and the workaround (forcing a
SelectorEventLoop policy) only applies when the app is launched a
particular way -- it breaks the moment someone runs `uvicorn api.main:app`
directly. FastAPI runs `def` endpoints in a threadpool, so these queries
still execute concurrently without blocking the event loop, and the
behaviour is identical on Windows and Linux.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

_pool: ConnectionPool | None = None


def dsn() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is not set. See .env.example.")
    return value


def open_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn(),
            min_size=1,
            max_size=8,          # Neon's free tier is not generous; stay modest
            open=False,
            kwargs={"row_factory": dict_row},   # rows arrive as dicts, not tuples
        )
        _pool.open(wait=True, timeout=20)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query, return every row as a dict.

    params are passed separately and never interpolated into the SQL --
    the driver sends them out of band, so a value can never be parsed as
    SQL. That is what makes injection impossible, and it is the single
    most important habit in this file.
    """
    pool = open_pool()
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None
