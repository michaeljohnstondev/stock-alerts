-- ============================================================
-- 007_market_trends_shape.sql
--
-- 004 declared scan_runs.market_trend as a single TEXT value.
-- market.py:416 get_market_trend() returns a dict keyed by
-- benchmark: {"SPY": "bullish", "QQQ": "bullish"} -- one verdict
-- per benchmark, and the benchmark list is configurable
-- (config "market_alignment.benchmarks"), so the number of keys
-- is not fixed.
--
-- A configurable set of key/value verdicts is exactly the case
-- for JSONB rather than a column per benchmark.
-- ============================================================

ALTER TABLE scan_runs DROP COLUMN market_trend;
ALTER TABLE scan_runs ADD COLUMN market_trends JSONB NOT NULL DEFAULT '{}'::jsonb;
