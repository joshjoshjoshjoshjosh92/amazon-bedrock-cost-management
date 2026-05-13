-- Bedrock Usage View (CUR 1.0 / Legacy CUR)
-- Creates a filtered view of Bedrock-specific cost data from the Cost and Usage Report.
-- Uses Legacy CUR column naming conventions.
--
-- Parameters:
--   :database - Glue database name (e.g., bct_cur_database)
--   :table - CUR table name (e.g., bct_bedrock_cur)
--
-- Usage:
--   Replace :database and :table with your actual Glue catalog names,
--   then execute in Athena to create the view.

CREATE OR REPLACE VIEW bedrock_usage AS
SELECT
    line_item_usage_account_id AS account_id,
    line_item_usage_start_date AS usage_hour,
    product_region AS region,
    line_item_resource_id AS resource_id,
    line_item_operation AS operation,
    line_item_usage_type AS usage_type,
    line_item_unblended_cost AS unblended_cost_usd,
    COALESCE(line_item_net_unblended_cost, line_item_unblended_cost) AS effective_cost_usd,
    savings_plan_savings_plan_effective_cost AS sp_effective_cost_usd,
    resource_tags_user_team AS team,
    resource_tags_user_application AS application,
    resource_tags_user_environment AS environment
FROM :database.:table
WHERE line_item_product_code = 'AmazonBedrock'
  AND line_item_line_item_type IN ('Usage', 'SavingsPlanCoveredUsage', 'SavingsPlanNegation', 'DiscountedUsage');
