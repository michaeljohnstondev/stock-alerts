"""Migration runner.

Applies every .sql file in migrations/ that hasn't been applied yet,
in filename order, each inside its own transaction. Already-applied
files are skipped, so running this is always safe.

    python db/migrate.py            # apply pending migrations
    python db/migrate.py --status   # show what's applied
"""
from __future__ import annotations

import sys
from pathlib import Path

from connect import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# The bootstrap table: the database's own record of which migrations
# have run. Everything else in the schema is created by a migration;
# this one table has to exist first.
BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def applied_set(conn) -> set[str]:
    conn.execute(BOOTSTRAP)
    conn.commit()
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def show_status() -> None:
    with connect() as conn:
        done = applied_set(conn)
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("No migration files found.")
        return
    for path in files:
        mark = "applied" if path.name in done else "PENDING"
        print(f"  [{mark:>7}]  {path.name}")


def migrate() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("No migration files found.")
        return

    with connect() as conn:
        done = applied_set(conn)
        pending = [p for p in files if p.name not in done]

        if not pending:
            print(f"Up to date — {len(done)} migration(s) already applied.")
            return

        for path in pending:
            sql = path.read_text(encoding="utf-8")
            print(f"applying {path.name} ...", end=" ", flush=True)
            try:
                # Each migration runs as ONE transaction. If any statement
                # in the file fails, the whole file rolls back and the
                # database is left exactly as it was.
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
                conn.commit()
                print("ok")
            except Exception as exc:
                conn.rollback()
                print("FAILED")
                print(f"\n{path.name} rolled back. Nothing was changed.\n{exc}")
                sys.exit(1)

        print(f"\nApplied {len(pending)} migration(s).")


if __name__ == "__main__":
    if "--status" in sys.argv:
        show_status()
    else:
        migrate()
