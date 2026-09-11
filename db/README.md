# Postgres layer

Gives `ratings_runner` a memory. The scanner computes prices, alert
states, and value grades on every run and then throws almost all of it
away; this layer keeps it.

Additive by design. The JSON state files are still written exactly as
before, and if `DATABASE_URL` is unset or the database is unreachable,
the report runs normally and prints one warning.

## Why

`.alert_state.json` is a **state snapshot** — it stores the deepest rung
per ticker per side and overwrites itself every run. That answers exactly
one question ("deeper than last time?") and forgets everything else.

`alert_events` is an **append-only log**. The current state becomes a
query over it:

```sql
SELECT DISTINCT ON (symbol, direction) symbol, direction, rung
FROM alert_events
WHERE triggered = TRUE
ORDER BY symbol, direction, observed_at DESC;
```

Same answer — plus *how long has this been triggered*, *how often does
this name escalate*, and *what did price do next*, none of which a
snapshot can answer at any price.

The database also records **every rung evaluated**, not just the ones
that reach the report. The report shows the deepest unmuted rung by
design; the history keeps the full picture.

## Setup

1. Create a free Postgres at [neon.tech](https://neon.tech).
2. Put the connection string in `.env`:
   ```
   DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
   ```
3. `pip install -r requirements.txt`
4. `python db/migrate.py` — create the schema
5. `python db/import_json.py` — load existing JSON state (idempotent)

Then run the scanner as usual (`python -m stock_checker`) and each run
records itself.

## Layout

| File | Purpose |
|---|---|
| `connect.py` | Single connection point. Transaction context manager. |
| `migrate.py` | Applies pending `migrations/*.sql` in order, one transaction each. `--status` to inspect. |
| `import_json.py` | Re-runnable UPSERT import of the JSON state files. |
| `writer.py` | Called by `ratings_runner.run_report()`. Fail-soft. |
| `q.py` | Query runner for exploration. Always rolls back. |

## Schema

Imported from JSON:

| Table | Source |
|---|---|
| `tickers` | `.sector_cache.json` |
| `rating_levels` | `.user_ratings.json` — buys + sells flattened, one row per rung |
| `fundamentals` | `.fundamental_cache.json` — typed columns + JSONB overflow |
| `suppressions` | `.skipped.json` + `.muted.json` |

Written per run, and previously not persisted at all:

| Table | What it keeps |
|---|---|
| `scan_runs` | One row per run. Children cascade from it. |
| `price_observations` | The live price each run fetches and discards. |
| `alert_events` | Every rung evaluated, with `pct_away` / `triggered` / `escalated`. |
| `value_scores` | `score_value()` output — grade plus four sub-scores. |
| `sector_snapshots` | Accumulates instead of overwriting `.sector_state.json`. |

## Migrations

Numbered, applied in order, never edited once applied. Schema changes go
in a new file.

| | |
|---|---|
| `001_core.sql` | Initial schema. |
| `002_widen_ratio_precision.sql` | Ratios aren't bounded to 0–1 in practice (BEAM: -258 operating margin). |
| `003_fix_legacy_drift.sql` | Outcome is `draw` not `scratch`; dead `tracking` status; missing `earnings_gap`. |
| `004_current_system.sql` | Dropped the legacy picks modeling; added the tables the live scanner needs. |
| `005_value_direction_terms.sql` | `bullish`/`bearish`, matching `scoring.py`. |
| `006_link_runs.sql` | Foreign keys to `scan_runs` — the relation was implicit before. |
