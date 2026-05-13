"""Restore Access Lambda function.

Removes IAM deny policies or SCPs from a target account to restore Bedrock access
after budget review. Requires confirmAccountId body field matching the path parameter
and a reason field. Logs restoration events to the audit table with the restoring
principal's ARN, timestamp, and reason.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

try:
    from functions.shared.constants import DENIED_ACTIONS
except ImportError:
    from shared.constants import DENIED_ACTIONS

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "bedrock-cost-sentry-audit-log")
BUDGETS_TABLE = os.environ.get("BUDGETS_TABLE", "bedrock-cost-sentry-budgets")
POLICY_NAME = "BedrockCostSentryDeny"
SCP_NAME_PREFIX = "BedrockCostSentry"

# Clients
dynamodb = boto3.resource("dynamodb")
iam = boto3.client("iam")
organizations = boto3.client("organizations")


def lambda_handler(event, context):
    """Restore Bedrock access for a previously shut-off account.

    Validates the request, removes the deny policy/SCP, and logs the
    restoration to the audit table.

    Args:
        event: API Gateway proxy event with account ID and confirmation details.
            - pathParameters.account_id: Target account ID
            - body: {confirmAccountId: str, reason: str}
            - requestContext.identity.userArn: Restoring principal's ARN

        context: Lambda execution context.

    Returns:
        dict: API Gateway proxy response with restoration status.
    """
    # Extract path parameter
    path_parameters = event.get("pathParameters") or {}
    account_id = path_parameters.get("account_id", "")

    if not account_id:
        return _response(400, {"error": "account_id path parameter is required"})

    # Parse request body
    body = _parse_body(event)
    if body is None:
        return _response(400, {"error": "Invalid JSON in request body"})

    # Validate confirmAccountId matches path parameter (typo prevention)
    confirm_account_id = body.get("confirmAccountId", "")
    if not confirm_account_id:
        return _response(400, {
            "error": "confirmAccountId is required in request body",
            "detail": "Must match the account_id in the URL path for safety confirmation",
        })

    if confirm_account_id != account_id:
        return _response(400, {
            "error": "confirmAccountId does not match path parameter",
            "detail": "The confirmAccountId in the body must exactly match the account_id in the URL",
            "path_account_id": account_id,
            "body_confirm_account_id": confirm_account_id,
        })

    # Validate reason field
    reason = body.get("reason", "").strip()
    if not reason:
        return _response(400, {
            "error": "reason is required in request body",
            "detail": "Provide a justification for restoring access",
        })

    # Extract restoring principal's ARN
    request_context = event.get("requestContext") or {}
    identity = request_context.get("identity") or {}
    restoring_principal = identity.get("userArn", "unknown")

    logger.info(
        "Restore access request: account=%s principal=%s reason=%s",
        account_id,
        restoring_principal,
        reason,
    )

    # Perform the restoration
    restoration_result = restore_account_access(account_id)

    if not restoration_result["success"]:
        return _response(500, {
            "error": "Failed to restore access",
            "detail": restoration_result.get("error", "Unknown error"),
            "account_id": account_id,
        })

    # Log restoration to audit table
    _write_restore_audit(
        account_id=account_id,
        restoring_principal=restoring_principal,
        reason=reason,
        restoration_detail=restoration_result,
    )

    logger.info(
        "Access restored successfully: account=%s principal=%s",
        account_id,
        restoring_principal,
    )

    return _response(200, {
        "message": "Bedrock access restored successfully",
        "account_id": account_id,
        "restored_by": restoring_principal,
        "restoration_detail": {
            "iam_policies_removed": restoration_result.get("iam_policies_removed", 0),
            "scps_detached": restoration_result.get("scps_detached", 0),
        },
    })


def restore_account_access(account_id):
    """Remove deny policies and SCPs from the target account.

    Attempts both IAM policy removal and SCP detachment to handle
    either enforcement mechanism.

    Args:
        account_id: Target AWS account ID.

    Returns:
        dict: {success: bool, iam_policies_removed: int, scps_detached: int, error: str}
    """
    result = {
        "success": True,
        "iam_policies_removed": 0,
        "scps_detached": 0,
        "errors": [],
    }

    # Remove IAM deny policies
    iam_result = _remove_iam_deny_policies(account_id)
    result["iam_policies_removed"] = iam_result["removed"]
    if iam_result.get("errors"):
        result["errors"].extend(iam_result["errors"])

    # Remove SCPs
    scp_result = _remove_scp_enforcement(account_id)
    result["scps_detached"] = scp_result["detached"]
    if scp_result.get("errors"):
        result["errors"].extend(scp_result["errors"])

    # Consider success if at least one policy was removed or no errors
    if result["errors"] and result["iam_policies_removed"] == 0 and result["scps_detached"] == 0:
        result["success"] = False
        result["error"] = "; ".join(result["errors"])

    return result


def _remove_iam_deny_policies(account_id):
    """Remove BedrockCostSentryDeny inline policies from managed roles.

    Args:
        account_id: Target account ID.

    Returns:
        dict: {removed: int, errors: list}
    """
    removed = 0
    errors = []

    try:
        paginator = iam.get_paginator("list_roles")
        for page in paginator.paginate():
            for role in page["Roles"]:
                role_name = role["RoleName"]

                # Check if this role has our deny policy
                try:
                    iam.get_role_policy(
                        RoleName=role_name,
                        PolicyName=POLICY_NAME,
                    )
                    # Policy exists, remove it
                    iam.delete_role_policy(
                        RoleName=role_name,
                        PolicyName=POLICY_NAME,
                    )
                    removed += 1
                    logger.info(
                        "Removed deny policy from role=%s in account=%s",
                        role_name,
                        account_id,
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] == "NoSuchEntity":
                        # Policy doesn't exist on this role, skip
                        continue
                    else:
                        errors.append(f"Error checking role {role_name}: {str(e)}")

    except ClientError as e:
        errors.append(f"Failed to list roles: {str(e)}")

    return {"removed": removed, "errors": errors}


def _remove_scp_enforcement(account_id):
    """Detach and optionally delete Cost Sentry SCPs from the account.

    Args:
        account_id: Target account ID.

    Returns:
        dict: {detached: int, errors: list}
    """
    detached = 0
    errors = []

    try:
        # List policies attached to the account
        paginator = organizations.get_paginator("list_policies_for_target")
        for page in paginator.paginate(
            TargetId=account_id,
            Filter="SERVICE_CONTROL_POLICY",
        ):
            for policy in page["Policies"]:
                policy_name = policy.get("Name", "")
                policy_id = policy.get("Id", "")

                # Only detach Cost Sentry policies
                if policy_name.startswith(SCP_NAME_PREFIX):
                    try:
                        organizations.detach_policy(
                            PolicyId=policy_id,
                            TargetId=account_id,
                        )
                        detached += 1
                        logger.info(
                            "Detached SCP %s (%s) from account=%s",
                            policy_name,
                            policy_id,
                            account_id,
                        )
                    except ClientError as e:
                        errors.append(
                            f"Failed to detach SCP {policy_name}: {str(e)}"
                        )

    except ClientError as e:
        # Organizations API may not be available (not in an org)
        if e.response["Error"]["Code"] in (
            "AWSOrganizationsNotInUseException",
            "AccessDeniedException",
        ):
            logger.info("Organizations not available, skipping SCP removal")
        else:
            errors.append(f"Failed to list SCPs: {str(e)}")

    return {"detached": detached, "errors": errors}


def _write_restore_audit(account_id, restoring_principal, reason, restoration_detail):
    """Write a restoration audit record to the audit log table.

    Args:
        account_id: Target account ID.
        restoring_principal: ARN of the principal performing the restore.
        reason: Justification for restoring access.
        restoration_detail: Dict with restoration specifics.
    """
    table = dynamodb.Table(AUDIT_TABLE)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    month_key = now.strftime("%Y-%m")
    action_id = str(uuid.uuid4())

    # TTL: 13 months
    ttl_epoch = int(now.timestamp()) + (13 * 30 * 24 * 3600)

    record = {
        "PK": f"ACCOUNT#{account_id}",
        "SK": f"ACTION#{now_iso}#{action_id}",
        "action_type": f"restore#{month_key}",
        "team_id": "",  # May not be known at restore time
        "threshold_breached": "restore_requested",
        "spend_amount_usd": "0",
        "budget_limit_usd": "0",
        "enforcement_detail": {
            "reason": reason,
            "iam_policies_removed": str(restoration_detail.get("iam_policies_removed", 0)),
            "scps_detached": str(restoration_detail.get("scps_detached", 0)),
        },
        "initiated_by": restoring_principal,
        "restored_at": now_iso,
        "restored_by": restoring_principal,
        "ttl": ttl_epoch,
    }

    try:
        table.put_item(Item=record)
        logger.info(
            "Restore audit record written: account=%s principal=%s",
            account_id,
            restoring_principal,
        )
    except ClientError as e:
        logger.error("Failed to write restore audit record: %s", str(e))


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
        "body": json.dumps(body),
    }
