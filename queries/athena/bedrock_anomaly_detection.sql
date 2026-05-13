-- Bedrock Spend Anomaly Detection
-- Identifies days where spend deviates significantly from the rolling average.
-- Uses a 14-day rolling window and flags spend exceeding 2 standard deviations.
--
-- Parameters:
--   :start_date - Start date (YYYY-MM-DD)
--   :end_date - End date (YYYY-MM-DD)
--   :database - Glue database name
--   :table - CUR table name
--
-- Validates: Requirements 5.3, 12.3

WITH daily_spend AS (
    SELECT
        DATE_TRUNC('day', line_item_usage_start_date) AS day,
        resource_tags_user_team AS team,
        SUM(COALESCE(line_item_net_unblended_cost, line_item_unblended_cost)) AS effective_cost_usd
    FROM :database.:table
    WHERE line_item_product_code = 'AmazonBedrock'
      AND line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage', 'SavingsPlanNegation', 'DiscountedUsage')
      AND line_item_usage_start_date >= DATE_ADD('day', -14, CAST(:start_date AS DATE))
      AND line_item_usage_start_date < CAST(:end_date AS DATE)
    GROUP BY
        DATE_TRUNC('day', line_item_usage_start_date),
        resource_tags_user_team
),
rolling_stats AS (
    SELECT
        day,
        team,
        effective_cost_usd,
        AVG(effective_cost_usd) OVER (
            PARTITION BY team
            ORDER BY day
            ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
        ) AS rolling_avg,
        STDDEV(effective_cost_usd) OVER (
            PARTITION BY team
            ORDER BY day
            ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
        ) AS rolling_stddev
    FROM daily_spend
)
SELECT
    day,
    team,
    effective_cost_usd,
    rolling_avg,
    rolling_stddev,
    ROUND((effective_cost_usd - rolling_avg) / NULLIF(rolling_stddev, 0), 2) AS z_score,
    CASE
        WHEN effective_cost_usd > rolling_avg + (2 * rolling_stddev) THEN 'ANOMALY_HIGH'
        WHEN effective_cost_usd < rolling_avg - (2 * rolling_stddev) THEN 'ANOMALY_LOW'
        ELSE 'NORMAL'
    END AS anomaly_status
FROM rolling_stats
WHERE day >= CAST(:start_date AS DATE)
  AND rolling_avg IS NOT NULL
ORDER BY day DESC, z_score DESC;
