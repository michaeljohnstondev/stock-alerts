# stock-alerts

A screening toolkit for US equities. It walks a ticker universe, computes
indicators, scores what it finds against adaptive thresholds, and grades its own
earlier picks so the thresholds keep moving.

**Stack** — Python · pandas · NumPy · yfinance · Alpaca

## The pieces

| Entry point | What it does |
|---|---|
| `stock_checker/` | The scan: data fetch, indicators, signals, scoring, sector rotation, reporting |
| `suggest_levels.py` | Reads candidate buy levels off raw OHLCV |
| `fill_levels.py` | Builds entry ladders from those levels |
| `fill_sells.py` | The exit side of the same ladder |
| `roll_ladders.py` | Replaces a ladder price has broken through, keeping the old one on record |
| `rate_stocks.py` | Interactive review pass over the results |

### suggest_levels

Derives buy levels from price structure rather than from a fixed percentage:
consolidation shelves, pivot lows, unfilled gaps, and projected rising moving
averages. Candidates are ranked by confluence — how many independent methods
land on the same price.

### fill_levels

Turns those levels into four-rung entry ladders, spaced to each stock's own
realised volatility rather than a flat interval. Hand-set levels are never
overwritten; the ladder fills around them.

### rate_stocks

The interactive pass. Resumable across sessions, with muting, snoozing, and
staleness-driven re-rating so a name that was reviewed months ago comes back
around instead of sitting stale.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json     # tickers, indicators, email settings
cp .env.example .env                   # Alpaca credentials
python -m stock_checker
```

`config.json` and `.env` hold credentials and are gitignored.

## Where things live

Runtime files are gitignored and sorted by how much they matter:

| Folder | Holds | If lost |
|---|---|---|
| `state/` | Ratings and ladders, alert history, mutes, skips, sector snoozes, the shadow-ladder test | Gone - back it up |
| `cache/` | Fundamentals, sectors, FINRA dark pool files | Refetched on the next run |
| `backups/` | Dated copies of earlier ratings and config | Nothing breaks |
| `reports/` | Each run's report, plus a `_full` version listing every name | Nothing breaks |

Each run writes two reports: `ratings_<time>.txt` shows only what is new
since the last run, and `ratings_<time>_full.txt` shows every triggered and
approaching name with the new ones marked `*`. See `REPORT_GUIDE.md` for how
to read them.
