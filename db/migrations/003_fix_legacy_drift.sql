-- ============================================================
-- 003_fix_legacy_drift.sql
--
-- 001 was written by reading save_picks(), which shows what the
-- code WRITES, not what it actually uses. Auditing the read sites
-- turned up four mismatches:
--
--  1. Neutral outcome is "draw", not "scratch". The constant is
--     named SCRATCH_THRESHOLD but tracking.py:447 writes "draw".
--     Name drifted, value didn't.
--
--  2. status "tracking" is never written by any code path.
--     save_picks writes "open"; score_previous_picks writes
--     "partial"/"scored". tracking.py:37 still FILTERS on
--     "tracking" -- a dead value left over from a refactor.
--
--  3. earnings_gap was missed entirely. It isn't in save_picks --
--     adaptive.py:679 backfills it later, and it gates adaptive
--     learning (tracking.py:789 skips those picks).
--
--  4. Horizons are mutually exclusive by trade type, not all
--     three per pick: day trades score at 1d only, swing trades
--     at 5d and 10d only.
-- ============================================================

-- 1. draw, not scratch
ALTER TABLE pick_scores DROP CONSTRAINT pick_scores_outcome_check;
ALTER TABLE pick_scores ADD CONSTRAINT pick_scores_outcome_check
    CHECK (outcome IN ('win', 'loss', 'draw'));

-- 2. drop the dead status value
ALTER TABLE picks DROP CONSTRAINT picks_status_check;
ALTER TABLE picks ADD CONSTRAINT picks_status_check
    CHECK (status IN ('open', 'partial', 'scored'));

-- 3. the field that gates adaptive learning
--    NULL = not yet checked, FALSE = checked and clean, TRUE = gapped.
--    Three states, so this is nullable on purpose.
ALTER TABLE picks ADD COLUMN earnings_gap BOOLEAN;

-- 4. A GENERATED column: Postgres computes and stores this from
--    trade_type on every write. It cannot drift from its source,
--    because nothing can write to it directly.
ALTER TABLE picks ADD COLUMN is_day_trade BOOLEAN
    GENERATED ALWAYS AS (trade_type LIKE 'DAY TRADE%') STORED;

-- Which horizons are legal now follows from is_day_trade. Enforcing
-- it needs a cross-table rule, so it lives in a trigger rather than
-- a CHECK (a CHECK can only see its own row).
CREATE OR REPLACE FUNCTION enforce_horizon_matches_trade_type()
RETURNS TRIGGER AS $$
DECLARE
    day_trade BOOLEAN;
BEGIN
    SELECT is_day_trade INTO day_trade FROM picks WHERE id = NEW.pick_id;

    IF day_trade AND NEW.horizon_days <> 1 THEN
        RAISE EXCEPTION 'day trade pick % scored at %d; day trades score at 1d only',
            NEW.pick_id, NEW.horizon_days;
    END IF;
    IF NOT day_trade AND NEW.horizon_days = 1 THEN
        RAISE EXCEPTION 'swing pick % scored at 1d; swing trades score at 5d/10d only',
            NEW.pick_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_horizon_matches_trade_type
    BEFORE INSERT OR UPDATE ON pick_scores
    FOR EACH ROW EXECUTE FUNCTION enforce_horizon_matches_trade_type();

-- Partial index for the adaptive learning query: picks eligible to
-- learn from are non-C-tier, not excluded, and not earnings gaps.
CREATE INDEX idx_picks_adaptive ON picks (play_type, regime, direction)
    WHERE excluded = FALSE AND tier <> 'C' AND earnings_gap IS NOT TRUE;
