"""Spend Evaluator Lambda function.

Queries current spend from CloudWatch metrics (primary) or CUR via Athena (fallback),
compares against budget thresholds from DynamoDB, and returns an enforcement decision
(no_action, warn, throttle, shutoff). Uses a tiered data source hierarchy for spend
evaluation and implements deduplication via DynamoDB conditional writes.
"""

import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
BUDGETS_TABLE = os.environ.get("BUDGETS_TABLE", "bedrock-cost-sentry-budgets")
SPEND_HISTORY_TABLE = os.environ.get("SPEND_HISTORY_TABLE", "bedrock-cost-sentry-spend-history")
EVALUATION_LOCKS_TABLE = os.environ.get("EVALUATION_LOCKS_TABLE", "bedrock-cost-sentry-evaluation-locks")
CW_NAMESPACE = os.environ.get("CW_NAMESPACE", "BedrockCostTracker/Invocations")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "bedrock-cost-tracker")
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "bedrock_cur_db")
ROLLOVER_GRACE_MINUTES = int(os.environ.get("ROLLOVER_GRACE_MINUTES", "60"))

# Clients (initialized outside handler for reuse across invocations)
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
athena = boto3.client("athena")


def lambda_handler(event, context):
    """Evaluate current spend against budget thresholds.

    Args:
        event: Step Functions input containing budget and account details.
            Expected fields:
            - team_id: str
            - account_id: str
            - budget: dict with budget configuration
            - execution_id: str (Step Functions execution ID)

        context: Lambda execution context.

    Returns:
        dict: Enforcement decision with action, spend, limit, and percentage.
    """
    team_id = event["team_id"]
    account_id = event["account_id"]
    budget = event["budget"]
    execution_id = event.get("execution_id", context.aws_request_id)

    logger.info(
        "Evaluating spend for team=%s account=%s",
        team_id,
        account_id,
    )

    # Step 1: Acquire evaluation lock (deduplication)
    if not acquire_evaluation_lock(team_id, execution_id):
        logger.info("Evaluation lock not acquired for team=%s, skipping (duplicate)", team_id)
        return {
            "action": "no_action",
            "reason": "duplicate_evaluation",
            "team_id": team_id,
            "account_id": account_id,
        }

    # Step 2: Check for budget period rollover
    period_end = budget.get("period_end_date")
    if period_end:
        handle_period_rollover(team_id, account_id, budget, period_end)

    # Step 3: Check grace period
    if is_in_grace_period(budget):
        logger.info("Team %s is in rollover grace period, skipping enforcement", team_id)
        return {
            "action": "no_action",
            "reason": "grace_period",
            "team_id": team_id,
            "account_id": account_id,
        }

    # Step 4: Fetch current spend using tiered data source hierarchy
    spend_result = fetch_current_spend(team_id, account_id, budget)
    current_spend = spend_result["spend_usd"]
    data_source = spend_result["source"]

    # Step 5: Update CURRENT_PERIOD record with atomic ADD
    if spend_result.get("delta_usd", 0) > 0:
        update_current_period_spend(team_id, spend_result["delta_usd"])

    # Step 6: Determine enforcement action
    hard_limit = Decimal(str(budget["hard_limit_usd"]))
    enforcement_mode = budget.get("enforcement_mode", "shutoff")
    enforcement_scope = budget.get("enforcement_scope", "account-wide")

    decision = determine_enforcement_action(
        current_spend=current_spend,
        hard_limit=hard_limit,
        warning_threshold_pct=budget.get("warning_threshold_pct", 80),
        throttle_threshold_pct=budget.get("throttle_threshold_pct", 90),
        enforcement_mode=enforcement_mode,
    )

    result = {
        "action": decision["action"],
        "spend_amount_usd": float(current_spend),
        "budget_limit_usd": float(hard_limit),
        "spend_pct": float(
            (current_spend / hard_limit * 100) if hard_limit > 0 else Decimal("0")
        ),
        "team_id": team_id,
        "account_id": account_id,
        "enforcement_mode": enforcement_mode,
        "enforcement_scope": enforcement_scope,
        "data_source": data_source,
        "data_stale": spend_result.get("data_stale", False),
        "inference_profile_arns": budget.get("inference_profile_arns", []),
    }

    logger.info(
        "Enforcement decision: action=%s spend=%.2f limit=%.2f pct=%.1f%%",
        decision["action"],
        float(current_spend),
        float(hard_limit),
        result["spend_pct"],
    )

    return result


def acquire_evaluation_lock(team_id, execution_id):
    """Acquire evaluation lock via DynamoDB conditional write for deduplication.

    Uses conditional write: attribute_not_exists(last_evaluated_at) OR
    last_evaluated_at < :five_minutes_ago

    Args:
        team_id: Team identifier.
        execution_id: Step Functions execution ID.

    Returns:
        bool: True if lock acquired, False if already held.
    """
    table = dynamodb.Table(EVALUATION_LOCKS_TABLE)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    five_minutes_ago = (
        now.timestamp() - 300
    )
    five_minutes_ago_iso = datetime.fromtimestamp(
        five_minutes_ago, tz=timezone.utc
    ).isoformat()

    # TTL: auto-expire after 1 hour
    ttl_epoch = int(now.timestamp()) + 3600

    try:
        table.put_item(
            Item={
                "PK": f"TEAM#{team_id}",
                "last_evaluated_at": now_iso,
                "execution_id": execution_id,
                "ttl": ttl_epoch,
            },
            ConditionExpression=(
                "attribute_not_exists(last_evaluated_at) OR "
                "last_evaluated_at < :five_minutes_ago"
            ),
            ExpressionAttributeValues={
                ":five_minutes_ago": five_minutes_ago_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def handle_period_rollover(team_id, account_id, budget, period_end):
    """Detect and handle budget period rollover.

    Archives the final cumulative spend as a PERIOD_SUMMARY record and resets
    the CURRENT_PERIOD record for the new period. Uses conditional write
    (attribute_not_exists(SK)) on the summary record to prevent race conditions.

    Args:
        team_id: Team identifier.
        account_id: Account identifier.
        budget: Budget configuration dict.
        period_end: Period end date string (ISO 8601).
    """
    now = datetime.now(timezone.utc)

    try:
        period_end_dt = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning("Invalid period_end_date: %s, skipping rollover check", period_end)
        return

    if now <= period_end_dt:
        return  # Not yet past period end

    logger.info("Period rollover detected for team=%s, period_end=%s", team_id, period_end)

    spend_table = dynamodb.Table(SPEND_HISTORY_TABLE)

    # Read current period spend
    try:
        response = spend_table.get_item(
            Key={"PK": f"TEAM#{team_id}", "SK": "CURRENT_PERIOD"}
        )
        current_record = response.get("Item", {})
        cumulative_spend = current_record.get("cumulative_period_spend_usd", Decimal("0"))
    except ClientError as e:
        logger.error("Failed to read CURRENT_PERIOD for team=%s: %s", team_id, str(e))
        return

    # Archive as PERIOD_SUMMARY with conditional write for race prevention
    period_key = period_end_dt.strftime("%Y-%m")
    ttl_epoch = int(now.timestamp()) + (13 * 30 * 24 * 3600)  # ~13 months

    try:
        spend_table.put_item(
            Item={
                "PK": f"TEAM#{team_id}",
                "SK": f"PERIOD_SUMMARY#{period_key}",
                "cumulative_period_spend_usd": cumulative_spend,
                "period_end": period_end,
                "archived_at": now.isoformat(),
                "account_id": account_id,
                "ttl": ttl_epoch,
            },
            ConditionExpression="attribute_not_exists(SK)",
        )
        logger.info(
            "Archived period summary for team=%s period=%s spend=%.2f",
            team_id,
            period_key,
            float(cumulative_spend),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "Period summary already archived for team=%s period=%s (concurrent rollover)",
                team_id,
                period_key,
            )
            return
        raise

    # Reset CURRENT_PERIOD for new period
    try:
        spend_table.put_item(
            Item={
                "PK": f"TEAM#{team_id}",
                "SK": "CURRENT_PERIOD",
                "cumulative_period_spend_usd": Decimal("0"),
                "input_tokens": 0,
                "output_tokens": 0,
                "invocation_count": 0,
                "last_evaluated_at": now.isoformat(),
            }
        )
    except ClientError as e:
        logger.error("Failed to reset CURRENT_PERIOD for team=%s: %s", team_id, str(e))


def is_in_grace_period(budget):
    """Check if the current time is within the rollover grace period.

    Args:
        budget: Budget configuration dict with optional rollover_grace_minutes.

    Returns:
        bool: True if within grace period.
    """
    grace_minutes = budget.get("rollover_grace_minutes", ROLLOVER_GRACE_MINUTES)
    grace_minutes = max(0, min(1440, int(grace_minutes)))

    if grace_minutes == 0:
        return False

    period_start = budget.get("period_start_date")
    if not period_start:
        return False

    try:
        period_start_dt = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False

    now = datetime.now(timezone.utc)
    elapsed_minutes = (now - period_start_dt).total_seconds() / 60

    return 0 <= elapsed_minutes < grace_minutes


def fetch_current_spend(team_id, account_id, budget):
    """Fetch current spend using tiered data source hierarchy.

    Priority:
    1. CloudWatch Metrics (near real-time)
    2. CUR via Athena (8-24 hour lag)
    3. DynamoDB fallback (last known value)

    Args:
        team_id: Team identifier.
        account_id: Account identifier.
        budget: Budget configuration dict.

    Returns:
        dict: {spend_usd: Decimal, source: str, delta_usd: Decimal, data_stale: bool}
    """
    # Priority 1: CloudWatch Metrics
    cw_spend = _fetch_from_cloudwatch(team_id, account_id, budget)
    if cw_spend is not None:
        return {
            "spend_usd": cw_spend["total"],
            "delta_usd": cw_spend.get("delta", Decimal("0")),
            "source": "cloudwatch",
            "data_stale": False,
        }

    # Priority 2: CUR via Athena
    athena_spend = _fetch_from_athena(team_id, account_id, budget)
    if athena_spend is not None:
        return {
            "spend_usd": athena_spend,
            "delta_usd": Decimal("0"),
            "source": "athena",
            "data_stale": False,
        }

    # Priority 3: DynamoDB fallback (last known value)
    ddb_spend = _fetch_from_dynamodb(team_id)
    return {
        "spend_usd": ddb_spend,
        "delta_usd": Decimal("0"),
        "source": "dynamodb_fallback",
        "data_stale": True,
    }


def _fetch_from_cloudwatch(team_id, account_id, budget):
    """Query CloudWatch metrics for current spend.

    Args:
        team_id: Team identifier.
        account_id: Account identifier.
        budget: Budget configuration.

    Returns:
        dict with 'total' and 'delta' keys, or None if unavailable.
    """
    try:
        # Query the EstimatedCostUSD metric for this team
        period_start = budget.get("period_start_date")
        if not period_start:
            period_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ).isoformat()

        start_time = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
        end_time = datetime.now(timezone.utc)

        response = cloudwatch.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="EstimatedCostUSD",
            Dimensions=[
                {"Name": "Team", "Value": team_id},
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,  # 1-hour granularity
            Statistics=["Sum"],
        )

        datapoints = response.get("Datapoints", [])
        if not datapoints:
            logger.info("No CloudWatch datapoints for team=%s", team_id)
            return None

        total_spend = Decimal(str(sum(dp["Sum"] for dp in datapoints)))

        # Calculate delta from last known spend in DynamoDB
        last_known = _fetch_from_dynamodb(team_id)
        delta = max(Decimal("0"), total_spend - last_known)

        return {"total": total_spend, "delta": delta}

    except ClientError as e:
        logger.warning(
            "CloudWatch query failed for team=%s: %s", team_id, str(e)
        )
        return None
    except Exception as e:
        logger.warning(
            "Unexpected error querying CloudWatch for team=%s: %s", team_id, str(e)
        )
        return None


def _fetch_from_athena(team_id, account_id, budget):
    """Query CUR via Athena for spend data (reconciliation source).

    Args:
        team_id: Team identifier.
        account_id: Account identifier.
        budget: Budget configuration.

    Returns:
        Decimal spend amount, or None if unavailable.
    """
    try:
        period_start = budget.get("period_start_date")
        if not period_start:
            period_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ).strftime("%Y-%m-%d")

        query = (
            f"SELECT COALESCE(SUM(effective_cost_usd), 0) as total_spend "
            f"FROM {ATHENA_DATABASE}.bedrock_usage "
            f"WHERE line_item_product_code = 'AmazonBedrock' "
            f"AND line_item_usage_account_id = '{account_id}' "
            f"AND line_item_usage_start_date >= TIMESTAMP '{period_start}'"
        )

        response = athena.start_query_execution(
            QueryString=query,
            WorkGroup=ATHENA_WORKGROUP,
        )

        query_execution_id = response["QueryExecutionId"]

        # Wait for query to complete (with timeout)
        max_wait = 30  # seconds
        waited = 0
        while waited < max_wait:
            result = athena.get_query_execution(QueryExecutionId=query_execution_id)
            state = result["QueryExecution"]["Status"]["State"]

            if state == "SUCCEEDED":
                break
            elif state in ("FAILED", "CANCELLED"):
                logger.warning("Athena query %s for team=%s", state, team_id)
                return None

            time.sleep(2)
            waited += 2

        if waited >= max_wait:
            logger.warning("Athena query timed out for team=%s", team_id)
            return None

        # Get results
        results = athena.get_query_results(QueryExecutionId=query_execution_id)
        rows = results.get("ResultSet", {}).get("Rows", [])

        if len(rows) > 1:
            value = rows[1]["Data"][0].get("VarCharValue", "0")
            return Decimal(value)

        return None

    except ClientError as e:
        logger.warning("Athena query failed for team=%s: %s", team_id, str(e))
        return None
    except Exception as e:
        logger.warning(
            "Unexpected error querying Athena for team=%s: %s", team_id, str(e)
        )
        return None


def _fetch_from_dynamodb(team_id):
    """Fetch last known spend from DynamoDB CURRENT_PERIOD record.

    Args:
        team_id: Team identifier.

    Returns:
        Decimal: Last known cumulative spend, or 0 if not found.
    """
    try:
        table = dynamodb.Table(SPEND_HISTORY_TABLE)
        response = table.get_item(
            Key={"PK": f"TEAM#{team_id}", "SK": "CURRENT_PERIOD"}
        )
        item = response.get("Item")
        if item:
            return Decimal(str(item.get("cumulative_period_spend_usd", 0)))
        return Decimal("0")
    except ClientError as e:
        logger.warning("DynamoDB read failed for team=%s: %s", team_id, str(e))
        return Decimal("0")


def update_current_period_spend(team_id, delta_usd):
    """Update CURRENT_PERIOD record with atomic ADD operation.

    Uses DynamoDB ADD for cumulative_period_spend_usd increments.
    ADD is atomic and conflict-free with concurrent writers.

    Args:
        team_id: Team identifier.
        delta_usd: Spend amount to add (Decimal).
    """
    table = dynamodb.Table(SPEND_HISTORY_TABLE)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"PK": f"TEAM#{team_id}", "SK": "CURRENT_PERIOD"},
            UpdateExpression=(
                "ADD cumulative_period_spend_usd :delta "
                "SET last_evaluated_at = :now"
            ),
            ExpressionAttributeValues={
                ":delta": delta_usd,
                ":now": now_iso,
            },
        )
    except ClientError as e:
        logger.error(
            "Failed to update CURRENT_PERIOD for team=%s: %s", team_id, str(e)
        )


def determine_enforcement_action(
    current_spend,
    hard_limit,
    warning_threshold_pct,
    throttle_threshold_pct,
    enforcement_mode,
):
    """Determine the enforcement action based on spend vs budget thresholds.

    Decision logic:
    - no_action: spend < warning threshold
    - warn: spend >= warning threshold AND spend < throttle threshold
    - throttle: spend >= throttle threshold AND spend < hard limit
    - shutoff: spend >= hard limit

    The returned action never exceeds the enforcement_mode ceiling:
    - notify-only: max action is warn
    - throttle: max action is throttle
    - shutoff: max action is shutoff

    Threshold validation:
    - warning_threshold_pct < throttle_threshold_pct < 100
    - If misconfigured, log BUDGET_MISCONFIGURED and use higher value as effective

    Args:
        current_spend: Current spend amount (Decimal).
        hard_limit: Budget hard limit in USD (Decimal).
        warning_threshold_pct: Warning threshold as percentage of hard limit.
        throttle_threshold_pct: Throttle threshold as percentage of hard limit.
        enforcement_mode: One of 'notify-only', 'throttle', 'shutoff'.

    Returns:
        dict: {action: str} where action is one of no_action/warn/throttle/shutoff.
    """
    # Validate and normalize thresholds
    warning_pct = Decimal(str(warning_threshold_pct))
    throttle_pct = Decimal(str(throttle_threshold_pct))

    # Threshold validation: warning < throttle < 100
    if not (warning_pct < throttle_pct < Decimal("100")):
        logger.warning(
            "BUDGET_MISCONFIGURED: warning_threshold_pct=%s throttle_threshold_pct=%s "
            "- treating higher value as effective threshold",
            warning_pct,
            throttle_pct,
        )
        # If misconfigured, sort them and use higher as throttle
        if warning_pct >= throttle_pct:
            # Swap: use the higher value as throttle threshold
            warning_pct, throttle_pct = min(warning_pct, throttle_pct), max(warning_pct, throttle_pct)
        if throttle_pct >= Decimal("100"):
            throttle_pct = Decimal("99")
        if warning_pct >= throttle_pct:
            warning_pct = throttle_pct - Decimal("1")

    # Calculate threshold amounts
    warning_amount = hard_limit * warning_pct / Decimal("100")
    throttle_amount = hard_limit * throttle_pct / Decimal("100")

    # Determine raw action based on spend level
    if current_spend >= hard_limit:
        raw_action = "shutoff"
    elif current_spend >= throttle_amount:
        raw_action = "throttle"
    elif current_spend >= warning_amount:
        raw_action = "warn"
    else:
        raw_action = "no_action"

    # Apply enforcement_mode ceiling
    action = _apply_enforcement_ceiling(raw_action, enforcement_mode)

    return {"action": action}


def _apply_enforcement_ceiling(raw_action, enforcement_mode):
    """Apply enforcement mode ceiling to the raw action.

    The enforcement_mode defines the maximum action that can be taken:
    - notify-only: max is warn
    - throttle: max is throttle
    - shutoff: max is shutoff (no ceiling)

    Args:
        raw_action: The determined action before ceiling.
        enforcement_mode: The configured enforcement mode.

    Returns:
        str: The action after applying the ceiling.
    """
    action_severity = {"no_action": 0, "warn": 1, "throttle": 2, "shutoff": 3}
    mode_ceiling = {
        "notify-only": 1,  # max: warn
        "throttle": 2,     # max: throttle
        "shutoff": 3,      # max: shutoff
    }

    ceiling = mode_ceiling.get(enforcement_mode, 3)
    raw_severity = action_severity.get(raw_action, 0)

    if raw_severity > ceiling:
        # Downgrade to the ceiling action
        ceiling_actions = {0: "no_action", 1: "warn", 2: "throttle", 3: "shutoff"}
        return ceiling_actions[ceiling]

    return raw_action
