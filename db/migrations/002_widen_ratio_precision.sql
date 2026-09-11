-- ============================================================
-- 002_widen_ratio_precision.sql
--
-- 001 declared the margin / growth / ownership ratios as
-- NUMERIC(8,6) and NUMERIC(10,6), which caps them at +/-100
-- and +/-10000. Real data broke that immediately:
--
--   BEAM (pre-revenue biotech) operating_margins = -258.27
--   i.e. a -25,827% operating margin. Not bad data -- a company
--   with almost no revenue and large operating losses.
--
-- Ratios that are "normally 0 to 1" are not bounded in practice.
-- Widen to NUMERIC(14,6): 8 integer digits, same 6 decimals.
--
-- Widening a NUMERIC is a metadata-only change in Postgres -- no
-- table rewrite, no data loss, safe on a live table.
-- ============================================================

ALTER TABLE fundamentals
    ALTER COLUMN profit_margins    TYPE NUMERIC(14,6),
    ALTER COLUMN operating_margins TYPE NUMERIC(14,6),
    ALTER COLUMN return_on_equity  TYPE NUMERIC(14,6),
    ALTER COLUMN revenue_growth    TYPE NUMERIC(14,6),
    ALTER COLUMN earnings_growth   TYPE NUMERIC(14,6),
    ALTER COLUMN short_pct_float   TYPE NUMERIC(14,6);

ALTER TABLE picks
    ALTER COLUMN short_pct_float TYPE NUMERIC(14,6),
    ALTER COLUMN inst_pct        TYPE NUMERIC(14,6),
    ALTER COLUMN win_rate_est    TYPE NUMERIC(14,6);
