"""Budget API Lambda function.

Handles API Gateway requests for budget CRUD operations and spend queries.
Supports creating/updating/deleting budget definitions, querying current spend
and remaining budget, and retrieving the enforcement audit log with pagination.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
BUDGETS_TABLE = os.environ.get("BUDGETS_TABLE", "bedrock-cost-sentry-budgets")
SPEND_HISTORY_TABLE = os.environ.get("SPEND_HISTORY_TABLE", "bedrock-cost-sentry-spend-history")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "bedrock-cost-sentry-audit-log")
AUDIT_GSI_NAME = os.environ.get("AUDIT_GSI_NAME", "GSI1")

# Clients
dynamodb = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types from DynamoDB."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    """Handle API Gateway request for budget management operations.

    Routes:
        POST   /budgets           - Create/update budget definition
        DELETE /budgets/{budget_id} - Delete budget definition
        GET    /budgets/{team_id}  - Get team budget
        GET    /spend/{team_id}    - Query current spend and remaining budget
        GET    /audit              - Query enforcement audit log

    Args:
        event: API Gateway proxy event with HTTP method and path parameters.
        context: Lambda execution context.

    Returns:
        dict: API Gateway proxy response with status code and body.
    """
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_parameters = event.get("pathParameters") or {}

    logger.info("Budget API request: %s %s", http_method, path)

    try:
        # Route the request
        if path.startswith("/budgets") and http_method == "POST":
            return handle_create_budget(event)

        elif path.startswith("/budgets/") and http_method == "DELETE":
            budget_id = path_parameters.get("budget_id", "")
            return handle_delete_budget(budget_id, event)

        elif path.startswith("/budgets/") and http_method == "GET":
            team_id = path_parameters.get("team_id", "")
            return handle_get_budget(team_id)

        elif path.startswith("/spend/") and http_method == "GET":
            team_id = path_parameters.get("team_id", "")
            return handle_get_spend(team_id)

        elif path.startswith("/audit") and http_method == "GET":
            return handle_get_audit(event)

        else:
            return _response(404, {"error": "Not found", "path": path, "method": http_method})

    except Exception as e:
        logger.error("Unhandled error: %s", str(e), exc_info=True)
        return _response(500, {"error": "Internal server error"})


def handle_create_budget(event):
    """Create or update a budget definition.

    Validates threshold ordering: warning < throttle < 100.

    Args:
        event: API Gateway event with budget definition in body.

    Returns:
        dict: API Gateway response.
    """
    body = _parse_body(event)
    if body is None:
        return _response(400, {"error": "Invalid JSON in request body"})

    # Required fields
    team_id = body.get("team_id")
    hard_limit_usd = body.get("hard_limit_usd")

    if not team_id:
        return _response(400, {"error": "team_id is required"})
    if hard_limit_usd is None:
        return _response(400, {"error": "hard_limit_usd is required"})

    # Validate hard_limit_usd
    try:
        hard_limit = Decimal(str(hard_limit_usd))
        if hard_limit <= 0:
            return _response(400, {"error": "hard_limit_usd must be positive"})
    except (InvalidOperation, ValueError):
        return _response(400, {"error": "hard_limit_usd must be a valid number"})

    # Threshold validation: warning < throttle < 100
    warning_threshold_pct = body.get("warning_threshold_pct", 80)
    throttle_threshold_pct = body.get("throttle_threshold_pct", 90)

    try:
        warning_pct = float(warning_threshold_pct)
        throttle_pct = float(throttle_threshold_pct)
    except (TypeError, ValueError):
        return _response(400, {"error": "Threshold percentages must be valid numbers"})

    if not (0 < warning_pct < throttle_pct < 100):
        return _response(400, {
            "error": "Threshold ordering must be: 0 < warning_threshold_pct < throttle_threshold_pct < 100",
            "warning_threshold_pct": warning_pct,
            "throttle_threshold_pct": throttle_pct,
        })

    # Validate enforcement_mode
    enforcement_mode = body.get("enforcement_mode", "shutoff")
    valid_modes = ("notify-only", "throttle", "shutoff")
    if enforcement_mode not in valid_modes:
        return _response(400, {
            "error": f"enforcement_mode must be one of: {', '.join(valid_modes)}"
        })

    # Validate enforcement_scope
    enforcement_scope = body.get("enforcement_scope", "account-wide")
    valid_scopes = ("account-wide", "tag-scoped")
    if enforcement_scope not in valid_scopes:
        return _response(400, {
            "error": f"enforcement_scope must be one of: {', '.join(valid_scopes)}"
        })

    # Validate period
    period = body.get("period", "monthly")
    valid_periods = ("monthly", "quarterly", "annual")
    if period not in valid_periods:
        return _response(400, {
            "error": f"period must be one of: {', '.join(valid_periods)}"
        })

    # Generate or use provided budget_id
    budget_id = body.get("budget_id", str(uuid.uuid4()))
    now_iso = datetime.now(timezone.utc).isoformat()

    # Build budget item
    budget_item = {
        "PK": f"TEAM#{team_id}",
        "SK": f"BUDGET#{budget_id}",
        "budget_id": budget_id,
        "budget_name": body.get("budget_name", f"{team_id}-budget"),
        "team_id": team_id,
        "period": period,
        "hard_limit_usd": hard_limit,
        "warning_threshold_pct": Decimal(str(warning_pct)),
        "throttle_threshold_pct": Decimal(str(throttle_pct)),
        "enforcement_mode": enforcement_mode,
        "enforcement_scope": enforcement_scope,
        "shutoff_mechanism": body.get("shutoff_mechanism", "iam"),
        "sns_topic_arn": body.get("sns_topic_arn", ""),
        "finops_sns_topic_arn": body.get("finops_sns_topic_arn", ""),
        "account_ids": body.get("account_ids", []),
        "inference_profile_arns": body.get("inference_profile_arns", []),
        "anomaly_window_days": int(body.get("anomaly_window_days", 14)),
        "anomaly_threshold_multiplier": Decimal(str(body.get("anomaly_threshold_multiplier", 2.0))),
        "rollover_grace_minutes": int(body.get("rollover_grace_minutes", 60)),
        "updated_at": now_iso,
    }

    # Check if this is a create or update
    table = dynamodb.Table(BUDGETS_TABLE)
    existing = None
    try:
        response = table.get_item(Key={"PK": f"TEAM#{team_id}", "SK": f"BUDGET#{budget_id}"})
        existing = response.get("Item")
    except ClientError:
        pass

    if existing:
        budget_item["created_at"] = existing.get("created_at", now_iso)
    else:
        budget_item["created_at"] = now_iso

    # Write budget
    try:
        table.put_item(Item=budget_item)
    except ClientError as e:
        logger.error("Failed to write budget: %s", str(e))
        return _response(500, {"error": "Failed to save budget"})

    # Write audit record for budget modification
    _write_budget_audit(
        team_id=team_id,
        budget_id=budget_id,
        action="created" if not existing else "updated",
        old_values=existing,
        new_values=budget_item,
    )

    logger.info("Budget %s for team=%s: budget_id=%s",
                "updated" if existing else "created", team_id, budget_id)

    return _response(200 if existing else 201, {
        "message": f"Budget {'updated' if existing else 'created'} successfully",
        "budget_id": budget_id,
        "team_id": team_id,
    })


def handle_delete_budget(budget_id, event):
    """Delete a budget definition with audit logging.

    Args:
        budget_id: Budget ID to delete.
        event: API Gateway event.

    Returns:
        dict: API Gateway response.
    """
    if not budget_id:
        return _response(400, {"error": "budget_id is required"})

    # We need team_id to construct the key - check query params or body
    query_params = event.get("queryStringParameters") or {}
    team_id = query_params.get("team_id", "")

    if not team_id:
        # Try to find the budget by scanning (less efficient but handles the case)
        body = _parse_body(event)
        if body:
            team_id = body.get("team_id", "")

    if not team_id:
        return _response(400, {"error": "team_id is required (query parameter or body)"})

    table = dynamodb.Table(BUDGETS_TABLE)

    # Fetch existing budget for audit
    try:
        response = table.get_item(Key={"PK": f"TEAM#{team_id}", "SK": f"BUDGET#{budget_id}"})
        existing = response.get("Item")
    except ClientError as e:
        logger.error("Failed to fetch budget for deletion: %s", str(e))
        return _response(500, {"error": "Failed to fetch budget"})

    if not existing:
        return _response(404, {"error": "Budget not found", "budget_id": budget_id})

    # Delete the budget
    try:
        table.delete_item(Key={"PK": f"TEAM#{team_id}", "SK": f"BUDGET#{budget_id}"})
    except ClientError as e:
        logger.error("Failed to delete budget: %s", str(e))
        return _response(500, {"error": "Failed to delete budget"})

    # Write audit record
    _write_budget_audit(
        team_id=team_id,
        budget_id=budget_id,
        action="deleted",
        old_values=existing,
        new_values=None,
    )

    logger.info("Budget deleted: team=%s budget_id=%s", team_id, budget_id)

    return _response(200, {
        "message": "Budget deleted successfully",
        "budget_id": budget_id,
        "team_id": team_id,
    })


def handle_get_budget(team_id):
    """Retrieve a team's budget definition.

    Args:
        team_id: Team identifier.

    Returns:
        dict: API Gateway response with budget data.
    """
    if not team_id:
        return _response(400, {"error": "team_id is required"})

    table = dynamodb.Table(BUDGETS_TABLE)

    try:
        response = table.query(
            KeyConditionExpression=Key("PK").eq(f"TEAM#{team_id}") & Key("SK").begins_with("BUDGET#"),
        )
        items = response.get("Items", [])
    except ClientError as e:
        logger.error("Failed to query budgets for team=%s: %s", team_id, str(e))
        return _response(500, {"error": "Failed to retrieve budgets"})

    if not items:
        return _response(404, {"error": "No budgets found for team", "team_id": team_id})

    # Return all budgets for the team
    budgets = []
    for item in items:
        budget = _sanitize_budget_response(item)
        budgets.append(budget)

    return _response(200, {"team_id": team_id, "budgets": budgets})


def handle_get_spend(team_id):
    """Query current spend and remaining budget for a team.

    Reads the CURRENT_PERIOD record from spend-history table.

    Args:
        team_id: Team identifier.

    Returns:
        dict: API Gateway response with spend data.
    """
    if not team_id:
        return _response(400, {"error": "team_id is required"})

    spend_table = dynamodb.Table(SPEND_HISTORY_TABLE)
    budgets_table = dynamodb.Table(BUDGETS_TABLE)

    # Fetch current period spend
    try:
        response = spend_table.get_item(
            Key={"PK": f"TEAM#{team_id}", "SK": "CURRENT_PERIOD"}
        )
        spend_record = response.get("Item")
    except ClientError as e:
        logger.error("Failed to query spend for team=%s: %s", team_id, str(e))
        return _response(500, {"error": "Failed to retrieve spend data"})

    if not spend_record:
        return _response(404, {"error": "No spend data found for team", "team_id": team_id})

    # Fetch budget to calculate remaining
    try:
        budget_response = budgets_table.query(
            KeyConditionExpression=Key("PK").eq(f"TEAM#{team_id}") & Key("SK").begins_with("BUDGET#"),
            Limit=1,
        )
        budget_items = budget_response.get("Items", [])
    except ClientError:
        budget_items = []

    cumulative_spend = Decimal(str(spend_record.get("cumulative_period_spend_usd", 0)))
    budget_limit = Decimal("0")
    if budget_items:
        budget_limit = Decimal(str(budget_items[0].get("hard_limit_usd", 0)))

    remaining = max(Decimal("0"), budget_limit - cumulative_spend)
    spend_pct = (cumulative_spend / budget_limit * 100) if budget_limit > 0 else Decimal("0")

    result = {
        "team_id": team_id,
        "cumulative_period_spend_usd": float(cumulative_spend),
        "budget_limit_usd": float(budget_limit),
        "remaining_budget_usd": float(remaining),
        "spend_pct": float(spend_pct),
        "input_tokens": int(spend_record.get("input_tokens", 0)),
        "output_tokens": int(spend_record.get("output_tokens", 0)),
        "invocation_count": int(spend_record.get("invocation_count", 0)),
        "last_evaluated_at": spend_record.get("last_evaluated_at", ""),
        "reconciled": spend_record.get("reconciled", False),
    }

    return _response(200, result)


def handle_get_audit(event):
    """Query enforcement audit log with date range filtering and pagination.

    Queries across monthly GSI partitions based on date range.
    Defaults to last 3 months if no date range specified.

    Args:
        event: API Gateway event with query parameters.

    Returns:
        dict: API Gateway response with audit records.
    """
    query_params = event.get("queryStringParameters") or {}

    # Parse parameters
    action_type = query_params.get("action_type", "")  # e.g., "shutoff", "warning"
    start_date = query_params.get("start_date", "")
    end_date = query_params.get("end_date", "")
    limit = min(int(query_params.get("limit", "50")), 100)
    next_token = query_params.get("next_token", "")
    account_id = query_params.get("account_id", "")

    # Default date range: last 3 months
    now = datetime.now(timezone.utc)
    if not end_date:
        end_date = now.strftime("%Y-%m-%d")
    if not start_date:
        # 3 months ago
        month = now.month - 3
        year = now.year
        if month <= 0:
            month += 12
            year -= 1
        start_date = f"{year}-{month:02d}-01"

    # Generate monthly partition keys to query
    partitions = _generate_monthly_partitions(action_type, start_date, end_date)

    table = dynamodb.Table(AUDIT_TABLE)
    all_items = []

    # If querying by account_id, use the primary key
    if account_id:
        try:
            query_kwargs = {
                "KeyConditionExpression": Key("PK").eq(f"ACCOUNT#{account_id}"),
                "Limit": limit,
                "ScanIndexForward": False,  # Most recent first
            }
            if next_token:
                query_kwargs["ExclusiveStartKey"] = json.loads(next_token)

            response = table.query(**query_kwargs)
            all_items = response.get("Items", [])
            last_key = response.get("LastEvaluatedKey")

        except ClientError as e:
            logger.error("Failed to query audit log: %s", str(e))
            return _response(500, {"error": "Failed to query audit log"})
    else:
        # Query across monthly GSI partitions
        for partition_key in partitions:
            if len(all_items) >= limit:
                break

            try:
                query_kwargs = {
                    "IndexName": AUDIT_GSI_NAME,
                    "KeyConditionExpression": Key("action_type").eq(partition_key),
                    "Limit": limit - len(all_items),
                    "ScanIndexForward": False,
                }

                response = table.query(**query_kwargs)
                all_items.extend(response.get("Items", []))

            except ClientError as e:
                logger.warning(
                    "Failed to query GSI partition %s: %s", partition_key, str(e)
                )
                continue

        last_key = None

    # Format response
    audit_records = []
    for item in all_items[:limit]:
        record = {
            "account_id": item.get("PK", "").replace("ACCOUNT#", ""),
            "action_type": item.get("action_type", ""),
            "team_id": item.get("team_id", ""),
            "threshold_breached": item.get("threshold_breached", ""),
            "spend_amount_usd": item.get("spend_amount_usd", "0"),
            "budget_limit_usd": item.get("budget_limit_usd", "0"),
            "initiated_by": item.get("initiated_by", ""),
            "restored_at": item.get("restored_at"),
            "restored_by": item.get("restored_by"),
            "timestamp": item.get("SK", "").replace("ACTION#", "").split("#")[0] if "ACTION#" in item.get("SK", "") else "",
        }
        audit_records.append(record)

    result = {
        "records": audit_records,
        "count": len(audit_records),
    }

    if last_key:
        result["next_token"] = json.dumps(last_key, cls=DecimalEncoder)

    return _response(200, result)


def _generate_monthly_partitions(action_type, start_date, end_date):
    """Generate monthly GSI partition keys for the given date range.

    Args:
        action_type: Action type filter (e.g., "shutoff"). If empty, queries all types.
        start_date: Start date string (YYYY-MM-DD).
        end_date: End date string (YYYY-MM-DD).

    Returns:
        list: Partition key strings (e.g., ["shutoff#2025-01", "shutoff#2025-02"]).
    """
    try:
        start = datetime.strptime(start_date[:7], "%Y-%m")
        end = datetime.strptime(end_date[:7], "%Y-%m")
    except (ValueError, TypeError):
        # Default to current month
        now = datetime.now(timezone.utc)
        start = end = now.replace(day=1)

    action_types = [action_type] if action_type else [
        "warning", "throttle", "shutoff", "restore", "budget_modified"
    ]

    partitions = []
    current = start
    while current <= end:
        month_key = current.strftime("%Y-%m")
        for at in action_types:
            partitions.append(f"{at}#{month_key}")
        # Move to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    return partitions


def _write_budget_audit(team_id, budget_id, action, old_values, new_values):
    """Write a budget modification audit record.

    Args:
        team_id: Team identifier.
        budget_id: Budget ID.
        action: Modification action (created, updated, deleted).
        old_values: Previous budget values (or None for create).
        new_values: New budget values (or None for delete).
    """
    table = dynamodb.Table(AUDIT_TABLE)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    month_key = now.strftime("%Y-%m")
    action_id = str(uuid.uuid4())

    # TTL: 13 months
    ttl_epoch = int(now.timestamp()) + (13 * 30 * 24 * 3600)

    # Extract account_id from budget if available
    account_id = "system"
    if new_values and new_values.get("account_ids"):
        account_ids = new_values["account_ids"]
        if isinstance(account_ids, list) and account_ids:
            account_id = account_ids[0]
    elif old_values and old_values.get("account_ids"):
        account_ids = old_values["account_ids"]
        if isinstance(account_ids, list) and account_ids:
            account_id = account_ids[0]

    enforcement_detail = {
        "budget_id": budget_id,
        "modification": action,
    }
    if old_values:
        enforcement_detail["old_values"] = {
            "hard_limit_usd": str(old_values.get("hard_limit_usd", "")),
            "warning_threshold_pct": str(old_values.get("warning_threshold_pct", "")),
            "throttle_threshold_pct": str(old_values.get("throttle_threshold_pct", "")),
            "enforcement_mode": str(old_values.get("enforcement_mode", "")),
        }
    if new_values:
        enforcement_detail["new_values"] = {
            "hard_limit_usd": str(new_values.get("hard_limit_usd", "")),
            "warning_threshold_pct": str(new_values.get("warning_threshold_pct", "")),
            "throttle_threshold_pct": str(new_values.get("throttle_threshold_pct", "")),
            "enforcement_mode": str(new_values.get("enforcement_mode", "")),
        }

    record = {
        "PK": f"ACCOUNT#{account_id}",
        "SK": f"ACTION#{now_iso}#{action_id}",
        "action_type": f"budget_modified#{month_key}",
        "team_id": team_id,
        "threshold_breached": f"budget_{action}",
        "spend_amount_usd": "0",
        "budget_limit_usd": str(new_values.get("hard_limit_usd", "0")) if new_values else "0",
        "enforcement_detail": enforcement_detail,
        "initiated_by": "api",
        "ttl": ttl_epoch,
    }

    try:
        table.put_item(Item=record)
        logger.info("Budget audit record written: %s budget_id=%s", action, budget_id)
    except ClientError as e:
        logger.error("Failed to write budget audit record: %s", str(e))


def _sanitize_budget_response(item):
    """Remove DynamoDB key attributes and format budget for API response.

    Args:
        item: DynamoDB item dict.

    Returns:
        dict: Cleaned budget response.
    """
    budget = {}
    skip_keys = {"PK", "SK"}
    for key, value in item.items():
        if key in skip_keys:
            continue
        if isinstance(value, Decimal):
            budget[key] = float(value) if value % 1 != 0 else int(value)
        elif isinstance(value, set):
            budget[key] = list(value)
        else:
            budget[key] = value
    return budget


def _parse_body(event):
    """Parse JSON body from API Gateway event.

    Args:
        event: API Gateway event.

    Returns:
        dict or None: Parsed body, or None if invalid.
    """
    body = event.get("body", "")
    if not body:
        return None
    try:
        if isinstance(body, str):
            return json.loads(body)
        return body
    except (json.JSONDecodeError, TypeError):
        return None


def _response(status_code, body):
    """Create an API Gateway proxy response.

    Args:
        status_code: HTTP status code.
        body: Response body dict.

    Returns:
        dict: API Gateway proxy response.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }
