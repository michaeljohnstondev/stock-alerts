"""Shared Postgres connection helper.

Reads DATABASE_URL from the project .env. Everything that touches
the database goes through here, so there's exactly one place that
knows how to connect.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to .env — see .env.example."
        )
    return dsn


@contextmanager
def connect():
    """Yield a connection that COMMITS on success and ROLLS BACK on error.

    This is the whole point of the migration away from JSON: a block
    of writes either all land or none do. No half-written state, and
    therefore no need for .bak files.
    """
    with psycopg.connect(get_dsn()) as conn:
        yield conn
