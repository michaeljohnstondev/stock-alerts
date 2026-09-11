-- ============================================================
-- 005_value_direction_terms.sql
--
-- 004 declared value_scores.direction IN ('long','short').
-- ratings_runner.py:66 calls score_value(fundamentals, "bullish"),
-- and scoring.py compares against "bullish"/"bearish" throughout.
-- Same drift as the 'draw'/'scratch' fix in 003: the schema used
-- the term I expected instead of the term the code uses.
--
-- The codebase is the authority. Match it.
-- ============================================================

ALTER TABLE value_scores DROP CONSTRAINT value_scores_direction_check;
ALTER TABLE value_scores ADD CONSTRAINT value_scores_direction_check
    CHECK (direction IN ('bullish', 'bearish'));
