-- ============================================================
-- 001_core.sql  —  stock-alerts core schema
--
-- Migrations are numbered, applied in order, and NEVER edited
-- once they've run. Changes go in 002_, 003_, and so on. That
-- file list becomes a complete, replayable history of the
-- database — the thing the .bak files were failing to be.
-- ============================================================

-- ------------------------------------------------------------
-- tickers — the master list. Everything else points back here.
-- ------------------------------------------------------------
CREATE TABLE tickers (
    symbol      TEXT PRIMARY KEY,      -- 'AAPL'. A natural key: it IS the identity.
    name        TEXT,
    sector      TEXT,
    industry    TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- rating_levels — replaces .user_ratings.json (494 tickers)
--
-- The JSON nests buys[] and sells[] under each ticker. Flattening
-- both into ONE table with a `direction` column means a single
-- query answers questions about either.
-- ------------------------------------------------------------
CREATE TABLE rating_levels (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    direction   TEXT NOT NULL CHECK (direction IN ('buy','sell')),
    rung        SMALLINT NOT NULL CHECK (rung BETWEEN 1 AND 8),
    price       NUMERIC(12,4) NOT NULL CHECK (price > 0),
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','auto')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One price per rung per direction per ticker. Duplicate rungs
    -- are now IMPOSSIBLE, not merely unlikely.
    UNIQUE (symbol, direction, rung)
);
CREATE INDEX idx_rating_levels_symbol ON rating_levels (symbol);

-- ------------------------------------------------------------
-- fundamentals — replaces .fundamental_cache.json (353 KB, 508 entries)
--
-- Hybrid design: the fields you actually filter and sort on get
-- real typed columns. Everything else lands in `raw` as JSONB so
-- a new yfinance field never requires a migration.
-- ------------------------------------------------------------
CREATE TABLE fundamentals (
    symbol            TEXT PRIMARY KEY REFERENCES tickers(symbol) ON DELETE CASCADE,
    fetched_at        TIMESTAMPTZ NOT NULL,   -- was the float `_timestamp`
    trailing_pe       NUMERIC(12,4),
    forward_pe        NUMERIC(12,4),
    peg_ratio         NUMERIC(12,4),
    profit_margins    NUMERIC(8,6),
    operating_margins NUMERIC(8,6),
    debt_to_equity    NUMERIC(12,4),
    return_on_equity  NUMERIC(10,6),
    revenue_growth    NUMERIC(10,6),
    earnings_growth   NUMERIC(10,6),
    free_cashflow     BIGINT,
    market_cap        BIGINT,
    short_pct_float   NUMERIC(8,6),
    beta              NUMERIC(8,4),
    target_mean_price NUMERIC(12,4),
    analyst_count     SMALLINT,
    recommendation    TEXT,
    raw               JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- Partial index: only rows that HAVE a market cap. Smaller and faster.
CREATE INDEX idx_fundamentals_mktcap ON fundamentals (market_cap DESC)
    WHERE market_cap IS NOT NULL;

-- ------------------------------------------------------------
-- picks — replaces .picks_history.json
--
-- The table the whole project turns on. One row per pick, INSERTed
-- once, UPDATEd in place as it's tracked. No more read-whole-file /
-- rewrite-whole-file at 12 different call sites.
-- ------------------------------------------------------------
CREATE TABLE picks (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol             TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    scan_date          DATE NOT NULL,
    picked_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    direction          TEXT NOT NULL CHECK (direction IN ('bullish','bearish')),

    -- classification
    tier               TEXT NOT NULL CHECK (tier IN ('A','B','C')),
    play_type          TEXT NOT NULL DEFAULT 'multi_signal',
    play_tags          TEXT[] NOT NULL DEFAULT '{}',   -- native array, no join table needed
    setup_context      TEXT NOT NULL DEFAULT 'with_trend',
    trade_type         TEXT,
    signal_indicators  TEXT[] NOT NULL DEFAULT '{}',

    -- the trade plan
    entry_price        NUMERIC(12,4) NOT NULL CHECK (entry_price > 0),
    stop               NUMERIC(12,4),
    tp1                NUMERIC(12,4),
    tp2                NUMERIC(12,4),

    -- scoring
    score              NUMERIC(10,4) NOT NULL,
    breakdown          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ev_ratio           NUMERIC(10,4),
    win_rate_est       NUMERIC(8,4),

    -- market context at pick time
    sector             TEXT,
    sector_status      TEXT,
    regime             TEXT NOT NULL DEFAULT 'unknown',
    regime_adx         NUMERIC(8,4),
    rs_raw             NUMERIC(12,4),
    vol_ratio          NUMERIC(10,4),
    distrib_vol_ratio  NUMERIC(10,4),

    -- market intelligence (feeds adaptive learning)
    short_pct_float    NUMERIC(8,6),
    float_shares       BIGINT,
    analyst_upside_pct NUMERIC(10,4),
    inst_pct           NUMERIC(8,6),
    beta               NUMERIC(8,4),

    -- lifecycle
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','tracking','partial','scored')),
    high_since         NUMERIC(12,4),
    low_since          NUMERIC(12,4),
    favorited          BOOLEAN NOT NULL DEFAULT FALSE,  -- doubles learning weight
    excluded           BOOLEAN NOT NULL DEFAULT FALSE,  -- zeroes learning weight
    exclude_reason     TEXT,

    -- Mirrors the existing dedupe rule: one active pick per
    -- (ticker, direction) per scan day. Re-running a scan is safe.
    UNIQUE (symbol, direction, scan_date),

    -- A pick is favorited OR excluded, never both — they're opposite
    -- learning weights. Enforced here so no code path can violate it.
    CHECK (NOT (favorited AND excluded)),
    CHECK (excluded = FALSE OR exclude_reason IS NOT NULL)
);
CREATE INDEX idx_picks_scan_date ON picks (scan_date DESC);
CREATE INDEX idx_picks_symbol    ON picks (symbol, scan_date DESC);
CREATE INDEX idx_picks_active    ON picks (status) WHERE status <> 'scored';
CREATE INDEX idx_picks_learnable ON picks (play_type, regime)
    WHERE excluded = FALSE AND tier <> 'C';
CREATE INDEX idx_picks_breakdown ON picks USING GIN (breakdown);
CREATE INDEX idx_picks_tags      ON picks USING GIN (play_tags);

-- ------------------------------------------------------------
-- pick_scores — the grading loop. TRACK_DAYS = [1, 5, 10]
--
-- The JSON stored result_1d / result_5d / result_10d as three
-- separate fields (plus three more exit_price_*d). That's a
-- "repeating group": the same fact recorded three times with the
-- number baked into the field NAME. The relational answer is rows,
-- not columns — add a 20-day horizon later and nothing changes here.
-- ------------------------------------------------------------
CREATE TABLE pick_scores (
    pick_id      BIGINT NOT NULL REFERENCES picks(id) ON DELETE CASCADE,
    horizon_days SMALLINT NOT NULL CHECK (horizon_days > 0),
    scored_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    exit_price   NUMERIC(12,4) NOT NULL CHECK (exit_price > 0),
    return_pct   NUMERIC(10,4) NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('win','loss','scratch')),

    -- Composite PK: a pick is graded exactly once per horizon.
    -- Re-running the scorer CANNOT create duplicates.
    PRIMARY KEY (pick_id, horizon_days)
);
CREATE INDEX idx_pick_scores_horizon ON pick_scores (horizon_days, outcome);

-- ------------------------------------------------------------
-- sector_snapshots — replaces .sector_state.json
-- That file holds only TODAY. A table keeps every day, so you can
-- finally ask how sector rotation moved over time.
-- ------------------------------------------------------------
CREATE TABLE sector_snapshots (
    captured_at TIMESTAMPTZ NOT NULL,
    sector      TEXT NOT NULL,
    rank        SMALLINT NOT NULL,
    composite   NUMERIC(10,4) NOT NULL,
    status      TEXT NOT NULL,
    close       NUMERIC(12,4),
    spy_close   NUMERIC(12,4),
    PRIMARY KEY (captured_at, sector)
);

-- ------------------------------------------------------------
-- alert_state — replaces .alert_state.json
-- ------------------------------------------------------------
CREATE TABLE alert_state (
    symbol     TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('buy','sell','buy_near','sell_near')),
    level      NUMERIC(12,4) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, kind)
);

-- ------------------------------------------------------------
-- suppressions — replaces .skipped.json and .muted.json
-- Two files, one shape. `until IS NULL` means suppressed forever.
-- ------------------------------------------------------------
CREATE TABLE suppressions (
    symbol TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    kind   TEXT NOT NULL CHECK (kind IN ('skipped','muted')),
    since  DATE NOT NULL DEFAULT CURRENT_DATE,
    until  DATE,
    reason TEXT,
    PRIMARY KEY (symbol, kind),
    CHECK (until IS NULL OR until >= since)   -- can't expire before it starts
);
