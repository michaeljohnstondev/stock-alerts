-- ============================================================
-- 004_current_system.sql
--
-- 001-003 modeled runner.py / tracking.py / adaptive.py -- the
-- picks-and-grading pipeline. That is legacy. The live entry
-- point is ratings_runner.main() (stock_checker/__main__.py),
-- which is a different system:
--
--   rating ladders (support/resistance)  -> already modeled
--   live price vs each rung              -> not stored at all
--   escalation "deeper than last time"   -> one overwritten file
--   value grade 0-100 (underpriced)      -> recomputed, discarded
--
-- Three of those four are thrown away on every run. This
-- migration stops discarding them.
-- ============================================================

-- ---- drop the legacy modeling (all empty, 0 rows) ----
DROP TRIGGER IF EXISTS trg_horizon_matches_trade_type ON pick_scores;
DROP FUNCTION IF EXISTS enforce_horizon_matches_trade_type();
DROP TABLE IF EXISTS pick_scores;
DROP TABLE IF EXISTS picks;

-- ------------------------------------------------------------
-- price_observations -- the time series everything else grades against.
--
-- ratings_runner fetches a live price for every rated ticker on
-- every run and then throws it away. Keeping it is what makes
-- "what happened AFTER this alert" answerable at all.
-- ------------------------------------------------------------
CREATE TABLE price_observations (
    symbol       TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    observed_at  TIMESTAMPTZ NOT NULL,
    price        NUMERIC(12,4) NOT NULL CHECK (price > 0),
    PRIMARY KEY (symbol, observed_at)
);

-- BRIN, not BTREE. Append-only data that arrives in time order
-- stores a tiny summary per block instead of an entry per row --
-- a fraction of the size, ideal for range scans over time.
CREATE INDEX idx_price_obs_time ON price_observations USING BRIN (observed_at);

-- ------------------------------------------------------------
-- alert_events -- an append-only log, replacing .alert_state.json
--
-- The state file answers exactly one question ("deeper than last
-- run?") and forgets everything else. A log answers that question
-- AND every other one, because the history is still there.
-- ------------------------------------------------------------
CREATE TABLE alert_events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol       TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    observed_at  TIMESTAMPTZ NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('buy','sell')),
    rung         SMALLINT NOT NULL CHECK (rung BETWEEN 1 AND 8),
    target_price NUMERIC(12,4) NOT NULL CHECK (target_price > 0),
    price        NUMERIC(12,4) NOT NULL CHECK (price > 0),
    pct_away     NUMERIC(10,4) NOT NULL,   -- negative = through the level
    triggered    BOOLEAN NOT NULL,
    escalated    BOOLEAN NOT NULL DEFAULT FALSE,
    prev_rung    SMALLINT,

    -- One observation per rung per run. Re-running the report on the
    -- same timestamp updates rather than duplicating.
    UNIQUE (symbol, direction, rung, observed_at)
);
CREATE INDEX idx_alert_events_symbol ON alert_events (symbol, observed_at DESC);
CREATE INDEX idx_alert_events_trig   ON alert_events (observed_at DESC)
    WHERE triggered = TRUE;
CREATE INDEX idx_alert_events_esc    ON alert_events (symbol, observed_at DESC)
    WHERE escalated = TRUE;

-- ------------------------------------------------------------
-- value_scores -- score_value() output, which is currently
-- recomputed every run and never written down.
--
-- Storing it makes the grade itself testable: do A-graded
-- triggers outperform D-graded ones? Right now that is unknowable.
-- ------------------------------------------------------------
CREATE TABLE value_scores (
    symbol        TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    observed_at   TIMESTAMPTZ NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('long','short')),
    grade         TEXT NOT NULL,
    score         SMALLINT CHECK (score BETWEEN 0 AND 100),
    valuation     SMALLINT,
    profitability SMALLINT,
    health        SMALLINT,
    growth        SMALLINT,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (symbol, observed_at, direction),

    -- The four sub-scores are 0-25 each and must sum to the total.
    -- scoring.py already guarantees this; now the database does too.
    CHECK (score IS NULL OR score = valuation + profitability + health + growth)
);
CREATE INDEX idx_value_scores_grade ON value_scores (grade, observed_at DESC);

-- ------------------------------------------------------------
-- scan_runs -- one row per `python -m stock_checker`.
-- Gives every row above a run to belong to, and makes
-- "compare this run to the last one" a trivial query.
-- ------------------------------------------------------------
CREATE TABLE scan_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    tickers_scanned  INTEGER,
    api_calls        INTEGER,
    market_trend     TEXT,
    report_path      TEXT,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
