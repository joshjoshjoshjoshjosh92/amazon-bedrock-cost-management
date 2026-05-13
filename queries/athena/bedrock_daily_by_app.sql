-- Bedrock Daily Spend by Application
-- Returns daily cost breakdown grouped by application tag.
--
-- Parameters:
--   :start_date - Start date (YYYY-MM-DD)
--   :end_date - End date (YYYY-MM-DD)
--   :database - Glue database name
--   :table - CUR table name
--
-- Validates: Requirements 4.1, 4.2

SELECT
    resource_tags_user_application AS application,
    DATE_TRUNC('day', line_item_usage_start_date) AS day,
    SUM(COALESCE(line_item_net_unblended_cost, line_item_unblended_cost)) AS effective_cost_usd,
    SUM(line_item_unblended_cost) AS unblended_cost_usd,
    COUNT(*) AS line_item_count
FROM :database.:table
WHERE line_item_product_code = 'AmazonBedrock'
  AND line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage', 'SavingsPlanNegation', 'DiscountedUsage')
  AND line_item_usage_start_date >= CAST(:start_date AS DATE)
  AND line_item_usage_start_date < CAST(:end_date AS DATE)
GROUP BY
    resource_tags_user_application,
    DATE_TRUNC('day', line_item_usage_start_date)
ORDER BY
    day DESC,
    effective_cost_usd DESC;
