"""Reconciliation Handler Lambda function.

Performs daily comparison of CloudWatch-derived spend against CUR ground truth
via Athena. If drift exceeds the threshold (5% OR $500 absolute, whichever is lower),
updates the DynamoDB spend record with a conditional write and emits a
SpendReconciliationDrift CloudWatch metric. Triggers immediate enforcement evaluation
if corrected spend breaches a new threshold.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
BUDGETS_TABLE = os.environ.get("BUDGETS_TABLE", "bedrock-cost-sentry-budgets")
SPEND_HISTORY_TABLE = os.environ.get("SPEND_HISTORY_TABLE", "bedrock-cost-sentry-spend-history")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "bedrock-cost-tracker")
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "bedrock_cur_db")
CW_NAMESPACE = os.environ.get("CW_NAMESPACE", "BedrockCostTracker")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
DEPLOYMENT_TIMESTAMP_KEY = os.environ.get("DEPLOYMENT_TIMESTAMP_KEY", "DEPLOYMENT_METADATA")

# Drift thresholds
DRIFT_THRESHOLD_PCT = Decimal(os.environ.get("DRIFT_THRESHOLD_PCT", "5"))
DRIFT_THRESHOLD_ABSOLUTE_USD = Decimal(os.environ.get("DRIFT_THRESHOLD_ABSOLUTE_USD", "500"))

# Bootstrap period: skip execution for first 48 hours after deployment
BOOTSTRAP_HOURS = int(os.environ.get("BOOTSTRAP_HOURS", "48"))

# Athena query timeout
ATHENA_QUERY_TIMEOUT_SECONDS = int(os.environ.get("ATHENA_QUERY_TIMEOUT_SECONDS", "60"))

# Clients
dynamodb = boto3.resource("dynamodb")
athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")
stepfunctions = boto3.client("stepfunctions")


def lambda_handler(event, context):
    """Reconcile CloudWatch-derived spend against CUR ground truth.

    Steps:
    1. Check deployment timestamp - skip if < 48h (bootstrap period)
    2. For each team/account with a budget, query Athena for CUR ground truth
    3. Compare against CURRENT_PERIOD record in DynamoDB
    4. If drift > threshold, update with conditional write
    5. Emit SpendReconciliationDrift metric
    6. If corrected spend breaches a threshold, trigger Step Functions

    Args:
        event: EventBridge scheduled event.
        context: Lambda execution context.

    Returns:
        dict: Reconciliation results with drift details.
    """
    logger.info("Starting spend reconciliation")

    # Step 1: Check bootstrap period
    bootstrap_status = check_bootstrap_period()
    if bootstrap_status == "BOOTSTRAP_NO_DATA":
        logger.info("Within bootstrap period (<%dh since deployment), skipping reconciliation", BOOTSTRAP_HOURS)
        return {
            "status": "BOOTSTRAP_NO_DATA",
            "message": f"Skipping reconciliation - within {BOOTSTRAP_HOURS}h bootstrap period",
        }

    # Step 2: Fetch all budgets
    budgets = fetch_all_budgets()
    if not budgets:
        logger.info("No budgets found, nothing to reconcile")
        return {"status": "no_budgets", "reconciled": 0, "drifts_detected": 0}

    results = {
        "status": "completed",
        "reconciled": 0,
        "drifts_detected": 0,
        "enforcement_triggered": 0,
        "errors": 0,
        "details": [],
    }

    # Step 3-6: Process each budget
    for budget in budgets:
        team_id = budget.get("team_id", "")
        account_ids = budget.get("account_ids", [])

        if not team_id:
            continue

        # Process each account under this budget
        for account_id in account_ids:
            try:
                detail = reconcile_team_account(team_id, account_id, budget)
                results["reconciled"] += 1

                if detail.get("drift_detected"):
                    results["drifts_detected"] += 1
                if detail.get("enforcement_triggered"):
                    results["enforcement_triggered"] += 1

                results["details"].append(detail)

            except Exception as e:
                logger.error(
                    "Reconciliation failed for team=%s account=%s: %s",
                    team_id,
                    account_id,
                    str(e),
                )
                results["errors"] += 1
                results["details"].append({
                    "team_id": team_id,
                    "account_id": account_id,
                    "error": str(e),
                })

        # If no specific account_ids, reconcile at team level
        if not account_ids:
            try:
                detail = reconcile_team_account(team_id, "", budget)
                results["reconciled"] += 1

                if detail.get("drift_detected"):
                    results["drifts_detected"] += 1
                if detail.get("enforcement_triggered"):
                    results["enforcement_triggered"] += 1

                results["details"].append(detail)

            except Exception as e:
                logger.error(
                    "Reconciliation failed for team=%s: %s", team_id, str(e)
                )
                results["errors"] += 1

    logger.info(
        "Reconciliation complete: reconciled=%d drifts=%d enforcement=%d errors=%d",
        results["reconciled"],
        results["drifts_detected"],
        results["enforcement_triggered"],
        results["errors"],
    )

    return results


def check_bootstrap_period():
    """Check if we're within the bootstrap period after deployment.

    Reads deployment_timestamp from the budgets table metadata record.
    If < 48 hours since deployment, returns BOOTSTRAP_NO_DATA.

    Returns:
        str: "BOOTSTRAP_NO_DATA" if within bootstrap period, "ready" otherwise.
    """
    table = dynamodb.Table(BUDGETS_TABLE)

    try:
        response = table.get_item(
            Key={"PK": "SYSTEM", "SK": DEPLOYMENT_TIMESTAMP_KEY}
        )
        item = response.get("Item")

        if not item:
            # No deployment timestamp found - assume ready (backwards compatibility)
            logger.info("No deployment timestamp found, proceeding with reconciliation")
            return "ready"

        deployment_timestamp = item.get("deployment_timestamp", "")
        if not deployment_timestamp:
            return "ready"

        deploy_dt = datetime.fromisoformat(deployment_timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours_since_deploy = (now - deploy_dt).total_seconds() / 3600

        if hours_since_deploy < BOOTSTRAP_HOURS:
            logger.info(
                "Bootstrap period: %.1f hours since deployment (threshold: %d hours)",
                hours_since_deploy,
                BOOTSTRAP_HOURS,
            )
            return "BOOTSTRAP_NO_DATA"

        return "ready"

    except ClientError as e:
        logger.warning("Failed to check deployment timestamp: %s", str(e))
        # On error, proceed with reconciliation (fail-open)
        return "ready"


def fetch_all_budgets():
    """Fetch all budget definitions from DynamoDB.

    Scans the budgets table for all BUDGET# records.

    Returns:
        list: Budget items.
    """
    table = dynamodb.Table(BUDGETS_TABLE)
    budgets = []

    try:
        # Scan for all budget records (filter for BUDGET# SK prefix)
        paginator = table.meta.client.get_paginator("scan")
        for page in paginator.paginate(
            TableName=BUDGETS_TABLE,
            FilterExpression="begins_with(SK, :prefix)",
            ExpressionAttributeValues={":prefix": {"S": "BUDGET#"}},
        ):
            for item in page.get("Items", []):
                # Convert DynamoDB format to simple dict
                budget = _deserialize_item(item)
                budgets.append(budget)

    except ClientError as e:
        logger.error("Failed to fetch budgets: %s", str(e))

    # Also try using the resource interface
    if not budgets:
        try:
            response = table.scan(
                FilterExpression="begins_with(SK, :prefix)",
                ExpressionAttributeValues={":prefix": "BUDGET#"},
            )
            budgets = response.get("Items", [])

            # Handle pagination
            while response.get("LastEvaluatedKey"):
                response = table.scan(
                    FilterExpression="begins_with(SK, :prefix)",
                    ExpressionAttributeValues={":prefix": "BUDGET#"},
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                budgets.extend(response.get("Items", []))

        except ClientError as e:
            logger.error("Failed to fetch budgets (resource): %s", str(e))

    return budgets


def reconcile_team_account(team_id, account_id, budget):
    """Reconcile spend for a specific team/account combination.

    Args:
        team_id: Team identifier.
        account_id: Account ID (may be empty for team-level reconciliation).
        budget: Budget configuration dict.

    Returns:
        dict: Reconciliation detail with drift information.
    """
    # Get current DynamoDB spend record
    spend_table = dynamodb.Table(SPEND_HISTORY_TABLE)
    try:
        response = spend_table.get_item(
            Key={"PK": f"TEAM#{team_id}", "SK": "CURRENT_PERIOD"}
        )
        current_record = response.get("Item")
    except ClientError as e:
        logger.error("Failed to read CURRENT_PERIOD for team=%s: %s", team_id, str(e))
        raise

    if not current_record:
        logger.info("No CURRENT_PERIOD record for team=%s, skipping", team_id)
        return {
            "team_id": team_id,
            "account_id": account_id,
            "drift_detected": False,
            "reason": "no_current_period_record",
        }

    cw_spend = Decimal(str(current_record.get("cumulative_period_spend_usd", 0)))
    last_reconciled_at = current_record.get("reconciled_at", "")

    # Query Athena for CUR ground truth
    period_start = _get_period_start(budget)
    cur_spend = query_athena_spend(team_id, account_id, period_start)

    if cur_spend is None:
        logger.warning("Athena query returned no data for team=%s", team_id)
        return {
            "team_id": team_id,
            "account_id": account_id,
            "drift_detected": False,
            "reason": "athena_no_data",
        }

    # Calculate drift
    drift_absolute = abs(cur_spend - cw_spend)
    drift_pct = (
        (drift_absolute / cw_spend * Decimal("100")) if cw_spend > 0
        else Decimal("100") if cur_spend > 0
        else Decimal("0")
    )

    # Determine if drift exceeds threshold (5% OR $500, whichever is LOWER)
    exceeds_pct = drift_pct > DRIFT_THRESHOLD_PCT
    exceeds_absolute = drift_absolute > DRIFT_THRESHOLD_ABSOLUTE_USD
    drift_detected = exceeds_pct or exceeds_absolute

    detail = {
        "team_id": team_id,
        "account_id": account_id,
        "cw_spend_usd": float(cw_spend),
        "cur_spend_usd": float(cur_spend),
        "drift_absolute_usd": float(drift_absolute),
        "drift_pct": float(drift_pct),
        "drift_detected": drift_detected,
        "enforcement_triggered": False,
    }

    if not drift_detected:
        logger.info(
            "No significant drift for team=%s: cw=$%.2f cur=$%.2f drift=%.1f%%/$%.2f",
            team_id,
            float(cw_spend),
            float(cur_spend),
            float(drift_pct),
            float(drift_absolute),
        )
        return detail

    logger.info(
        "Drift detected for team=%s: cw=$%.2f cur=$%.2f drift=%.1f%%/$%.2f",
        team_id,
        float(cw_spend),
        float(cur_spend),
        float(drift_pct),
        float(drift_absolute),
    )

    # Step 4: Update with conditional write
    update_success = update_spend_with_reconciliation(
        team_id=team_id,
        corrected_spend=cur_spend,
        last_reconciled_at=last_reconciled_at,
    )

    if not update_success:
        detail["update_status"] = "conditional_write_failed"
        # Retry once after 5 seconds
        time.sleep(5)
        update_success = update_spend_with_reconciliation(
            team_id=team_id,
            corrected_spend=cur_spend,
            last_reconciled_at=last_reconciled_at,
        )
        if not update_success:
            detail["update_status"] = "retry_failed"
            logger.warning("Conditional write failed after retry for team=%s", team_id)
            return detail

    detail["update_status"] = "success"

    # Step 5: Emit SpendReconciliationDrift metric
    emit_drift_metric(team_id, drift_absolute, drift_pct)

    # Step 6: Check if corrected spend breaches a new threshold
    hard_limit = Decimal(str(budget.get("hard_limit_usd", 0)))
    warning_pct = Decimal(str(budget.get("warning_threshold_pct", 80)))
    throttle_pct = Decimal(str(budget.get("throttle_threshold_pct", 90)))

    if hard_limit > 0:
        # Determine thresholds
        warning_amount = hard_limit * warning_pct / Decimal("100")
        throttle_amount = hard_limit * throttle_pct / Decimal("100")

        # Check if corrected spend crosses a threshold that CW spend didn't
        new_breach = _detect_new_threshold_breach(
            old_spend=cw_spend,
            new_spend=cur_spend,
            warning_amount=warning_amount,
            throttle_amount=throttle_amount,
            hard_limit=hard_limit,
        )

        if new_breach:
            logger.info(
                "Corrected spend breaches new threshold for team=%s: %s",
                team_id,
                new_breach,
            )
            trigger_enforcement(team_id, account_id, budget, cur_spend)
            detail["enforcement_triggered"] = True
            detail["new_threshold_breached"] = new_breach

    return detail


def query_athena_spend(team_id, account_id, period_start):
    """Query CUR via Athena for ground-truth spend data.

    Args:
        team_id: Team identifier.
        account_id: Account ID (may be empty).
        period_start: Period start date string (YYYY-MM-DD).

    Returns:
        Decimal: Total spend from CUR, or None if query fails.
    """
    # Build query with appropriate filters
    where_clauses = [
        "line_item_product_code = 'AmazonBedrock'",
        f"line_item_usage_start_date >= TIMESTAMP '{period_start}'",
    ]

    if account_id:
        where_clauses.append(f"line_item_usage_account_id = '{account_id}'")

    # Use team tag if available
    if team_id:
        where_clauses.append(f"resource_tags_user_team = '{team_id}'")

    where_clause = " AND ".join(where_clauses)

    query = (
        f"SELECT COALESCE(SUM("
        f"COALESCE(line_item_net_unblended_cost, line_item_unblended_cost)"
        f"), 0) as total_spend "
        f"FROM {ATHENA_DATABASE}.cur_table "
        f"WHERE {where_clause}"
    )

    try:
        response = athena.start_query_execution(
            QueryString=query,
            WorkGroup=ATHENA_WORKGROUP,
        )
        query_execution_id = response["QueryExecutionId"]

        # Wait for query completion
        waited = 0
        while waited < ATHENA_QUERY_TIMEOUT_SECONDS:
            result = athena.get_query_execution(QueryExecutionId=query_execution_id)
            state = result["QueryExecution"]["Status"]["State"]

            if state == "SUCCEEDED":
                break
            elif state in ("FAILED", "CANCELLED"):
                reason = result["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
                logger.warning(
                    "Athena query %s for team=%s: %s", state, team_id, reason
                )
                return None

            time.sleep(2)
            waited += 2

        if waited >= ATHENA_QUERY_TIMEOUT_SECONDS:
            logger.warning("Athena query timed out for team=%s", team_id)
            return None

        # Get results
        results = athena.get_query_results(QueryExecutionId=query_execution_id)
        rows = results.get("ResultSet", {}).get("Rows", [])

        if len(rows) > 1:
            value = rows[1]["Data"][0].get("VarCharValue", "0")
            return Decimal(value)

        return Decimal("0")

    except ClientError as e:
        logger.error("Athena query failed for team=%s: %s", team_id, str(e))
        return None


def update_spend_with_reconciliation(team_id, corrected_spend, last_reconciled_at):
    """Update CURRENT_PERIOD record with CUR ground truth using conditional write.

    Condition: attribute_not_exists(reconciled_at) OR reconciled_at = :last_known

    Args:
        team_id: Team identifier.
        corrected_spend: Corrected spend amount from CUR (Decimal).
        last_reconciled_at: Last known reconciled_at value for optimistic locking.

    Returns:
        bool: True if update succeeded, False if conditional check failed.
    """
    table = dynamodb.Table(SPEND_HISTORY_TABLE)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        if last_reconciled_at:
            # Subsequent reconciliation: use optimistic locking
            condition = "reconciled_at = :last_known"
            expr_values = {
                ":spend": corrected_spend,
                ":now": now_iso,
                ":reconciled": True,
                ":last_known": last_reconciled_at,
            }
        else:
            # First reconciliation: reconciled_at doesn't exist yet
            condition = "attribute_not_exists(reconciled_at)"
            expr_values = {
                ":spend": corrected_spend,
                ":now": now_iso,
                ":reconciled": True,
            }

        table.update_item(
            Key={"PK": f"TEAM#{team_id}", "SK": "CURRENT_PERIOD"},
            UpdateExpression=(
                "SET cumulative_period_spend_usd = :spend, "
                "reconciled_at = :now, "
                "reconciled = :reconciled"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=expr_values,
        )
        logger.info(
            "Reconciliation update successful for team=%s: spend=$%.2f",
            team_id,
            float(corrected_spend),
        )
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Conditional write failed for team=%s (concurrent modification)",
                team_id,
            )
            return False
        raise


def emit_drift_metric(team_id, drift_absolute, drift_pct):
    """Emit SpendReconciliationDrift CloudWatch metric.

    Args:
        team_id: Team identifier.
        drift_absolute: Absolute drift amount in USD.
        drift_pct: Drift as percentage.
    """
    try:
        cloudwatch.put_metric_data(
            Namespace=CW_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "SpendReconciliationDrift",
                    "Dimensions": [
                        {"Name": "TeamId", "Value": team_id},
                    ],
                    "Value": float(drift_absolute),
                    "Unit": "None",
                },
                {
                    "MetricName": "SpendReconciliationDriftPct",
                    "Dimensions": [
                        {"Name": "TeamId", "Value": team_id},
                    ],
                    "Value": float(drift_pct),
                    "Unit": "Percent",
                },
            ],
        )
        logger.info(
            "Emitted drift metric for team=%s: $%.2f (%.1f%%)",
            team_id,
            float(drift_absolute),
            float(drift_pct),
        )
    except ClientError as e:
        logger.error("Failed to emit drift metric: %s", str(e))


def trigger_enforcement(team_id, account_id, budget, corrected_spend):
    """Trigger immediate enforcement evaluation via Step Functions.

    Args:
        team_id: Team identifier.
        account_id: Account ID.
        budget: Budget configuration.
        corrected_spend: Corrected spend amount.
    """
    if not STATE_MACHINE_ARN:
        logger.warning("STATE_MACHINE_ARN not configured, cannot trigger enforcement")
        return

    execution_input = {
        "team_id": team_id,
        "account_id": account_id,
        "budget": {
            "hard_limit_usd": float(budget.get("hard_limit_usd", 0)),
            "warning_threshold_pct": float(budget.get("warning_threshold_pct", 80)),
            "throttle_threshold_pct": float(budget.get("throttle_threshold_pct", 90)),
            "enforcement_mode": budget.get("enforcement_mode", "shutoff"),
            "enforcement_scope": budget.get("enforcement_scope", "account-wide"),
            "inference_profile_arns": budget.get("inference_profile_arns", []),
            "sns_topic_arn": budget.get("sns_topic_arn", ""),
            "finops_sns_topic_arn": budget.get("finops_sns_topic_arn", ""),
        },
        "trigger": "reconciliation_drift",
        "corrected_spend_usd": float(corrected_spend),
    }

    # Use team_id + hour for deduplication
    now = datetime.now(timezone.utc)
    execution_name = f"{team_id}-reconciliation-{now.strftime('%Y-%m-%d-%H')}"
    # Step Functions execution names must be <= 80 chars and match [a-zA-Z0-9-_]
    execution_name = execution_name[:80].replace(" ", "-")

    try:
        stepfunctions.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=json.dumps(execution_input),
        )
        logger.info(
            "Triggered enforcement evaluation: team=%s execution=%s",
            team_id,
            execution_name,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ExecutionAlreadyExists":
            logger.info(
                "Enforcement execution already exists for team=%s (dedup)",
                team_id,
            )
        else:
            logger.error(
                "Failed to trigger enforcement for team=%s: %s",
                team_id,
                str(e),
            )


def _detect_new_threshold_breach(old_spend, new_spend, warning_amount, throttle_amount, hard_limit):
    """Detect if corrected spend crosses a threshold that old spend didn't.

    Args:
        old_spend: Previous (CW-derived) spend.
        new_spend: Corrected (CUR) spend.
        warning_amount: Warning threshold amount.
        throttle_amount: Throttle threshold amount.
        hard_limit: Hard limit amount.

    Returns:
        str or None: Name of newly breached threshold, or None.
    """
    # Check from highest to lowest severity
    if new_spend >= hard_limit and old_spend < hard_limit:
        return "shutoff"
    if new_spend >= throttle_amount and old_spend < throttle_amount:
        return "throttle"
    if new_spend >= warning_amount and old_spend < warning_amount:
        return "warning"
    return None


def _get_period_start(budget):
    """Get the period start date from budget configuration.

    Args:
        budget: Budget configuration dict.

    Returns:
        str: Period start date in YYYY-MM-DD format.
    """
    period_start = budget.get("period_start_date", "")
    if period_start:
        return period_start[:10]  # Trim to YYYY-MM-DD

    # Default to first of current month
    now = datetime.now(timezone.utc)
    return now.replace(day=1).strftime("%Y-%m-%d")


def _deserialize_item(item):
    """Deserialize a DynamoDB low-level item to a simple dict.

    Handles the case where scan returns items in DynamoDB wire format.

    Args:
        item: DynamoDB item (may be in wire format or already deserialized).

    Returns:
        dict: Simple dict with values.
    """
    # If item values are already simple types (from Table resource), return as-is
    if item and not any(isinstance(v, dict) and len(v) == 1 and list(v.keys())[0] in ("S", "N", "BOOL", "L", "M", "SS", "NS")
                        for v in item.values() if isinstance(v, dict)):
        return item

    # Otherwise deserialize from wire format
    from boto3.dynamodb.types import TypeDeserializer
    deserializer = TypeDeserializer()
    return {k: deserializer.deserialize(v) for k, v in item.items()}
