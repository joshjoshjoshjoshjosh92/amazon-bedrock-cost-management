-- Bedrock Year-over-Year Cost Comparison
-- Compares monthly Bedrock spend between the current year and previous year.
--
-- Parameters:
--   :start_date - Start date for current period (YYYY-MM-DD)
--   :end_date - End date for current period (YYYY-MM-DD)
--   :database - Glue database name
--   :table - CUR table name
--
-- Validates: Requirements 4.1, 4.4, 12.3

WITH current_year AS (
    SELECT
        DATE_TRUNC('month', line_item_usage_start_date) AS month,
        SUM(COALESCE(line_item_net_unblended_cost, line_item_unblended_cost)) AS effective_cost_usd
    FROM :database.:table
    WHERE line_item_product_code = 'AmazonBedrock'
      AND line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage', 'SavingsPlanNegation', 'DiscountedUsage')
      AND line_item_usage_start_date >= CAST(:start_date AS DATE)
      AND line_item_usage_start_date < CAST(:end_date AS DATE)
    GROUP BY DATE_TRUNC('month', line_item_usage_start_date)
),
previous_year AS (
    SELECT
        DATE_TRUNC('month', line_item_usage_start_date) AS month,
        SUM(COALESCE(line_item_net_unblended_cost, line_item_unblended_cost)) AS effective_cost_usd
    FROM :database.:table
    WHERE line_item_product_code = 'AmazonBedrock'
      AND line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage', 'SavingsPlanNegation', 'DiscountedUsage')
      AND line_item_usage_start_date >= DATE_ADD('year', -1, CAST(:start_date AS DATE))
      AND line_item_usage_start_date < DATE_ADD('year', -1, CAST(:end_date AS DATE))
    GROUP BY DATE_TRUNC('month', line_item_usage_start_date)
)
SELECT
    cy.month AS current_month,
    cy.effective_cost_usd AS current_cost_usd,
    py.effective_cost_usd AS previous_year_cost_usd,
    CASE
        WHEN py.effective_cost_usd > 0
        THEN ROUND((cy.effective_cost_usd - py.effective_cost_usd) / py.effective_cost_usd * 100, 2)
        ELSE NULL
    END AS yoy_change_pct
FROM current_year cy
LEFT JOIN previous_year py
    ON MONTH(cy.month) = MONTH(DATE_ADD('year', 1, py.month))
ORDER BY cy.month DESC;
