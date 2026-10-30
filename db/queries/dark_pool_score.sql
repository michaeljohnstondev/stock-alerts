-- Dark pool z-score in SQL - the same number darkpool.py computes.
--
--     python db/q.py db/queries/dark_pool_score.sql
--
-- The window frame is the whole idea:
--
--     ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
--
-- For each row, look at the 20 rows before it for the same symbol -
-- not including itself - and average them. That is "this stock's
-- normal", and it slides forward one day at a time. A self-join could
-- do the same thing; a window does it in one pass over sorted rows.
--
-- WINDOW w names the frame once so four aggregates can share it.

WITH scored AS (
    SELECT symbol,
           trade_date,
           short_ratio,
           AVG(short_ratio)          OVER w AS avg_ratio,
           STDDEV_SAMP(short_ratio)  OVER w AS sd_ratio,
           COUNT(*)                  OVER w AS days_of_history,
           total_volume / AVG(total_volume) OVER w AS vol_x
    FROM dark_pool_daily
    WINDOW w AS (PARTITION BY symbol ORDER BY trade_date
                 ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
),
latest AS (
    -- DISTINCT ON keeps the first row per symbol in ORDER BY order:
    -- here, each symbol's most recent day.
    SELECT DISTINCT ON (symbol) *,
           (short_ratio - avg_ratio) / NULLIF(sd_ratio, 0) AS z
    FROM scored
    WHERE days_of_history >= 10
    ORDER BY symbol, trade_date DESC
)
SELECT symbol,
       trade_date,
       ROUND(short_ratio * 100, 1) AS short_pct,
       ROUND(avg_ratio * 100, 1)   AS avg_pct,
       ROUND(z, 2)                 AS z,
       ROUND(vol_x, 2)             AS vol_x,
       -- Base score only. Streak and thin-volume adjustments live in
       -- darkpool.py; this is the part a window function teaches.
       CASE WHEN z >=  2.5  THEN 9
            WHEN z >=  2.0  THEN 8
            WHEN z >=  1.5  THEN 7
            WHEN z >=  0.75 THEN 6
            WHEN z >= -0.75 THEN 5
            WHEN z >= -1.5  THEN 4
            WHEN z >= -2.0  THEN 3
            WHEN z >= -2.5  THEN 2
            ELSE 1 END             AS base_score
FROM latest
WHERE z IS NOT NULL
ORDER BY ABS(z) DESC
LIMIT 25;
