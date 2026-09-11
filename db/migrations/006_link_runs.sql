-- ============================================================
-- 006_link_runs.sql
--
-- 004 created scan_runs and said every row would "belong to a run",
-- then never added the foreign key. The tables were related only by
-- a shared observed_at timestamp -- an implicit join on a value,
-- which is exactly the kind of convention-not-constraint the JSON
-- files relied on.
--
-- Make it explicit. ON DELETE CASCADE also means deleting a bad run
-- cleans up everything it wrote, in one statement.
-- ============================================================

ALTER TABLE price_observations
    ADD COLUMN run_id BIGINT REFERENCES scan_runs(id) ON DELETE CASCADE;
ALTER TABLE alert_events
    ADD COLUMN run_id BIGINT REFERENCES scan_runs(id) ON DELETE CASCADE;
ALTER TABLE value_scores
    ADD COLUMN run_id BIGINT REFERENCES scan_runs(id) ON DELETE CASCADE;

CREATE INDEX idx_price_obs_run    ON price_observations (run_id);
CREATE INDEX idx_alert_events_run ON alert_events (run_id);
CREATE INDEX idx_value_scores_run ON value_scores (run_id);
