-- ============================================================
-- 008_dark_pool_daily.sql
--
-- FINRA's daily off-exchange short volume, one row per symbol per
-- trading day. The scanner scores from its own file cache, so the
-- report never depends on this table -- it is here so the history
-- outlives the cache's ~7-week window.
--
-- Raw numbers only. The 1-10 score is DERIVED (it depends on the 20
-- days before each row), so it is a query over this table, not a
-- column in it. See db/queries/dark_pool_score.sql.
--
-- short_ratio is a GENERATED column: Postgres computes it from the
-- other two on every write, so it can never disagree with them.
-- ============================================================

CREATE TABLE dark_pool_daily (
    symbol          TEXT    NOT NULL REFERENCES tickers(symbol),
    trade_date      DATE    NOT NULL,
    short_volume    NUMERIC NOT NULL CHECK (short_volume >= 0),
    total_volume    NUMERIC NOT NULL CHECK (total_volume > 0),
    short_ratio     NUMERIC GENERATED ALWAYS AS (short_volume / total_volume) STORED,
    PRIMARY KEY (symbol, trade_date)
);

-- The primary key already indexes (symbol, trade_date), which is the
-- order every window over this table reads in. This one serves
-- "latest date loaded" and whole-market-on-a-day questions.
CREATE INDEX idx_dark_pool_date ON dark_pool_daily (trade_date);
