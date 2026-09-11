"""Tiny query runner — the learning tool.

    python db/q.py "SELECT * FROM tickers LIMIT 5"
    python db/q.py queries/win_rate.sql

Prints results as an aligned table. Read-only by convention: it never
commits, so anything you try is safe to experiment with.
"""
from __future__ import annotations

import sys
from pathlib import Path

from connect import connect


def render(cols: list[str], rows: list[tuple]) -> str:
    if not rows:
        return "(0 rows)"
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [
        max(len(c), max((len(row[i]) for row in cells), default=0))
        for i, c in enumerate(cols)
    ]
    sep = "-+-".join("-" * w for w in widths)
    head = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    body = "\n".join(
        " | ".join(v.ljust(w) for v, w in zip(row, widths)) for row in cells
    )
    return f"{head}\n{sep}\n{body}\n\n({len(rows)} row{'s' if len(rows) != 1 else ''})"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = " ".join(sys.argv[1:])
    path = Path(arg)
    sql = path.read_text(encoding="utf-8") if path.suffix == ".sql" and path.exists() else arg

    with connect() as conn:
        cur = conn.execute(sql)
        if cur.description is None:
            print(f"OK ({cur.rowcount} row(s) affected — NOT committed)")
        else:
            print(render([d.name for d in cur.description], cur.fetchall()))
        conn.rollback()


if __name__ == "__main__":
    main()
