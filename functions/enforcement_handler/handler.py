"""Enforcement Handler Lambda function.

Applies IAM deny policies or Service Control Policies (SCPs) to revoke Bedrock
invocation permissions when budget limits are breached. Implements a verify-after-apply
pattern to confirm enforcement is effective, and logs all actions to the audit table.
"""

import json
import logging
import os
import time
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
MANAGEMENT_ACCOUNT_ID = os.environ.get("MANAGEMENT_ACCOUNT_ID", "")
POLICY_NAME = "BedrockCostSentryDeny"
SCP_NAME_PREFIX = "BedrockCostSentry"

# Verification retry configuration
VERIFY_INITIAL_WAIT_SECONDS = 5
VERIFY_MAX_RETRIES = 3
VERIFY_BACKOFF_MULTIPLIER = 2  # 5s, 10s, 20s

# Clients
dynamodb = boto3.resource("dynamodb")
iam = boto3.client("iam")
organizations = boto3.client("organizations")


def lambda_handler(event, context):
    """Apply enforcement action (IAM deny policy or SCP) to target account.

    Args:
        event: Step Functions input containing enforcement decision and target details.
            Expected fields:
            - action: "shutoff" | "throttle"
            - account_id: target account
            - team_id: team identifier
            - enforcement_mode: "iam" | "scp"
            - enforcement_scope: "account-wide" | "tag-scoped"
            - inference_profile_arns: list of ARN patterns (for tag-scoped)
            - spend_amount_usd: current spend
            - budget_limit_usd: budget limit

        context: Lambda execution context.

    Returns:
        dict: Enforcement result with verification status and details.
    """
    action = event["action"]
    account_id = event["account_id"]
    team_id = event["team_id"]
    enforcement_mode = event.get("enforcement_mode", "iam")
    enforcement_scope = event.get("enforcement_scope", "account-wide")
    inference_profile_arns = event.get("inference_profile_arns", [])
    spend_amount_usd = event.get("spend_amount_usd", 0)
    budget_limit_usd = event.get("budget_limit_usd", 0)

    logger.info(
        "Applying enforcement: action=%s account=%s team=%s mode=%s scope=%s",
        action,
        account_id,
        team_id,
        enforcement_mode,
        enforcement_scope,
    )

    action_id = str(uuid.uuid4())
    enforcement_detail = {}

    try:
        # Step 1: Generate the deny policy
        policy_document = generate_deny_policy(enforcement_scope, inference_profile_arns, account_id)

        # Step 2: Apply the policy
        if enforcement_mode == "scp":
            enforcement_detail = apply_scp_enforcement(
                account_id, team_id, policy_document
            )
        else:
            # Default: IAM deny policy
            enforcement_detail = apply_iam_enforcement(
                account_id, team_id, policy_document
            )

        # Step 3: Verify enforcement (verify-after-apply pattern)
        verified = verify_enforcement(account_id, enforcement_mode)

        status = "confirmed" if verified else "unverified"
        enforcement_detail["verification_status"] = status

        logger.info(
            "Enforcement %s for account=%s: status=%s",
            action,
            account_id,
            status,
        )

    except Exception as e:
        logger.error(
            "Enforcement failed for account=%s: %s", account_id, str(e)
        )
        status = "failed"
        enforcement_detail["error"] = str(e)

    # Step 4: Log to audit table
    write_audit_record(
        action_id=action_id,
        action_type=action,
        account_id=account_id,
        team_id=team_id,
        spend_amount_usd=spend_amount_usd,
        budget_limit_usd=budget_limit_usd,
        enforcement_detail=enforcement_detail,
        status=status,
    )

    return {
        "status": status,
        "enforcement_detail": enforcement_detail,
        "action": action,
        "account_id": account_id,
        "team_id": team_id,
        "action_id": action_id,
    }


def generate_deny_policy(enforcement_scope, inference_profile_arns, account_id):
    """Generate the IAM deny policy JSON using DENIED_ACTIONS.

    Supports two scoping modes:
    - account-wide: Resource: * (blocks ALL Bedrock usage)
    - tag-scoped: Condition restricts deny to specific Inference Profile ARNs

    Args:
        enforcement_scope: "account-wide" or "tag-scoped"
        inference_profile_arns: List of ARN patterns for tag-scoped enforcement.
        account_id: Target account ID.

    Returns:
        dict: IAM policy document.
    """
    statement = {
        "Sid": "BedrockCostSentryDeny",
        "Effect": "Deny",
        "Action": DENIED_ACTIONS,
        "Resource": "*",
    }

    if enforcement_scope == "tag-scoped" and inference_profile_arns:
        # Add condition to restrict deny to specific Inference Profile ARNs
        arn_patterns = inference_profile_arns
        if not arn_patterns:
            arn_patterns = [
                f"arn:aws:bedrock:*:{account_id}:application-inference-profile/*"
            ]

        statement["Condition"] = {
            "ArnLike": {
                "bedrock:InferenceProfileArn": arn_patterns
                if len(arn_patterns) > 1
                else arn_patterns[0]
            }
        }

    policy_document = {
        "Version": "2012-10-17",
        "Statement": [statement],
    }

    return policy_document


def apply_iam_enforcement(account_id, team_id, policy_document):
    """Apply IAM deny policy to target account's Bedrock execution roles.

    Targets roles tagged with 'bedrock-cost-sentry:managed = true'.

    Args:
        account_id: Target account ID.
        team_id: Team identifier.
        policy_document: The deny policy document dict.

    Returns:
        dict: Enforcement details including affected roles.
    """
    affected_roles = []
    policy_json = json.dumps(policy_document)

    # List roles tagged with bedrock-cost-sentry:managed = true
    try:
        paginator = iam.get_paginator("list_roles")
        for page in paginator.paginate():
            for role in page["Roles"]:
                role_name = role["RoleName"]

                # Check if role has the managed tag
                if _role_has_managed_tag(role_name):
                    # Apply inline deny policy
                    iam.put_role_policy(
                        RoleName=role_name,
                        PolicyName=POLICY_NAME,
                        PolicyDocument=policy_json,
                    )
                    affected_roles.append(role_name)
                    logger.info(
                        "Applied deny policy to role=%s in account=%s",
                        role_name,
                        account_id,
                    )

    except ClientError as e:
        logger.error("Failed to apply IAM enforcement: %s", str(e))
        raise

    return {
        "mode": "iam",
        "policy_name": POLICY_NAME,
        "affected_roles": affected_roles,
        "policy_document": policy_document,
    }


def _role_has_managed_tag(role_name):
    """Check if a role has the bedrock-cost-sentry:managed = true tag.

    Args:
        role_name: IAM role name.

    Returns:
        bool: True if the role has the managed tag.
    """
    try:
        response = iam.list_role_tags(RoleName=role_name)
        for tag in response.get("Tags", []):
            if (
                tag["Key"] == "bedrock-cost-sentry:managed"
                and tag["Value"] == "true"
            ):
                return True
        return False
    except ClientError:
        return False


def apply_scp_enforcement(account_id, team_id, policy_document):
    """Apply SCP enforcement via AWS Organizations.

    Attaches a deny SCP to the target account. The SCP excludes the
    Cost Sentry's own enforcement role to prevent self-lockout.

    Args:
        account_id: Target account ID.
        team_id: Team identifier.
        policy_document: Base deny policy document dict.

    Returns:
        dict: Enforcement details including SCP ID.
    """
    # Modify policy for SCP: add exclusion for the enforcement role
    scp_policy = _generate_scp_policy(policy_document)
    scp_name = f"{SCP_NAME_PREFIX}-{team_id}-{account_id}"[:128]
    scp_description = (
        f"Bedrock Cost Sentry deny policy for team {team_id} "
        f"in account {account_id}"
    )[:512]

    policy_json = json.dumps(scp_policy)

    try:
        # Try to create the SCP
        response = organizations.create_policy(
            Content=policy_json,
            Description=scp_description,
            Name=scp_name,
            Type="SERVICE_CONTROL_POLICY",
        )
        policy_id = response["Policy"]["PolicySummary"]["Id"]

    except ClientError as e:
        if e.response["Error"]["Code"] == "DuplicatePolicyException":
            # Policy already exists, find and update it
            policy_id = _find_existing_scp(scp_name)
            if policy_id:
                organizations.update_policy(
                    PolicyId=policy_id,
                    Content=policy_json,
                )
            else:
                raise
        else:
            raise

    # Attach SCP to target account
    try:
        organizations.attach_policy(
            PolicyId=policy_id,
            TargetId=account_id,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "DuplicatePolicyAttachmentException":
            logger.info("SCP already attached to account=%s", account_id)
        else:
            raise

    logger.info(
        "Applied SCP enforcement: policy_id=%s account=%s", policy_id, account_id
    )

    return {
        "mode": "scp",
        "policy_id": policy_id,
        "policy_name": scp_name,
        "target_account": account_id,
    }


def _generate_scp_policy(base_policy):
    """Generate SCP policy with exclusion for the enforcement role.

    The SCP excludes the Cost Sentry's enforcement role in the management
    account to prevent self-lockout.

    Args:
        base_policy: Base deny policy document.

    Returns:
        dict: SCP policy document with exclusion condition.
    """
    statement = base_policy["Statement"][0].copy()
    statement["Sid"] = "BedrockCostSentryOrgDeny"

    # Add exclusion for the enforcement role
    if MANAGEMENT_ACCOUNT_ID:
        enforcer_arn = (
            f"arn:aws:iam::{MANAGEMENT_ACCOUNT_ID}:role/BedrockCostSentryEnforcer"
        )
        # Merge with existing conditions if any
        condition = statement.get("Condition", {})
        condition["ArnNotEquals"] = {
            "aws:PrincipalARN": enforcer_arn
        }
        statement["Condition"] = condition

    return {
        "Version": "2012-10-17",
        "Statement": [statement],
    }


def _find_existing_scp(scp_name):
    """Find an existing SCP by name.

    Args:
        scp_name: The SCP name to search for.

    Returns:
        str or None: Policy ID if found, None otherwise.
    """
    try:
        paginator = organizations.get_paginator("list_policies")
        for page in paginator.paginate(Filter="SERVICE_CONTROL_POLICY"):
            for policy in page["Policies"]:
                if policy["Name"] == scp_name:
                    return policy["Id"]
    except ClientError:
        pass
    return None


def verify_enforcement(account_id, enforcement_mode):
    """Verify enforcement is effective using SimulatePrincipalPolicy.

    Implements verify-after-apply pattern:
    - Wait 5s for IAM propagation
    - Call iam:SimulatePrincipalPolicy
    - Retry 3x with exponential backoff (5s, 10s, 20s)

    Args:
        account_id: Target account ID.
        enforcement_mode: "iam" or "scp".

    Returns:
        bool: True if enforcement verified, False otherwise.
    """
    # Find a managed role to verify against
    target_role_arn = _find_managed_role_arn(account_id)
    if not target_role_arn:
        logger.warning(
            "No managed role found for verification in account=%s", account_id
        )
        return False

    wait_seconds = VERIFY_INITIAL_WAIT_SECONDS

    for attempt in range(1, VERIFY_MAX_RETRIES + 1):
        # Wait for IAM propagation
        time.sleep(wait_seconds)

        try:
            response = iam.simulate_principal_policy(
                PolicySourceArn=target_role_arn,
                ActionNames=["bedrock:InvokeModel"],
                ResourceArns=["*"],
            )

            results = response.get("EvaluationResults", [])
            if results:
                eval_decision = results[0].get("EvalDecision", "")
                if eval_decision == "explicitDeny":
                    logger.info(
                        "Enforcement verified on attempt %d for account=%s",
                        attempt,
                        account_id,
                    )
                    return True

            logger.warning(
                "Verification attempt %d failed: decision=%s",
                attempt,
                results[0].get("EvalDecision", "unknown") if results else "no_results",
            )

        except ClientError as e:
            logger.warning(
                "Verification attempt %d error: %s", attempt, str(e)
            )

        # Exponential backoff: 5s, 10s, 20s
        wait_seconds *= VERIFY_BACKOFF_MULTIPLIER

    logger.error(
        "Enforcement verification failed after %d attempts for account=%s",
        VERIFY_MAX_RETRIES,
        account_id,
    )
    return False


def _find_managed_role_arn(account_id):
    """Find a role tagged with bedrock-cost-sentry:managed = true.

    Args:
        account_id: Target account ID.

    Returns:
        str or None: Role ARN if found, None otherwise.
    """
    try:
        paginator = iam.get_paginator("list_roles")
        for page in paginator.paginate():
            for role in page["Roles"]:
                role_name = role["RoleName"]
                if _role_has_managed_tag(role_name):
                    return role["Arn"]
    except ClientError:
        pass
    return None


def write_audit_record(
    action_id,
    action_type,
    account_id,
    team_id,
    spend_amount_usd,
    budget_limit_usd,
    enforcement_detail,
    status,
):
    """Write enforcement action record to the audit log table.

    Args:
        action_id: Unique action identifier (UUID).
        action_type: Type of action (shutoff, throttle).
        account_id: Target account ID.
        team_id: Team identifier.
        spend_amount_usd: Spend at time of action.
        budget_limit_usd: Budget limit at time of action.
        enforcement_detail: Dict with enforcement specifics.
        status: Enforcement status (confirmed, unverified, failed).
    """
    table = dynamodb.Table(AUDIT_TABLE)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    month_key = now.strftime("%Y-%m")

    # TTL: 13 months
    ttl_epoch = int(now.timestamp()) + (13 * 30 * 24 * 3600)

    # Sanitize enforcement_detail for DynamoDB (remove None values, convert types)
    sanitized_detail = _sanitize_for_dynamodb(enforcement_detail)

    record = {
        "PK": f"ACCOUNT#{account_id}",
        "SK": f"ACTION#{now_iso}#{action_id}",
        "action_type": f"{action_type}#{month_key}",
        "team_id": team_id,
        "threshold_breached": action_type,
        "spend_amount_usd": str(spend_amount_usd),
        "budget_limit_usd": str(budget_limit_usd),
        "enforcement_detail": sanitized_detail,
        "initiated_by": "system",
        "status": status,
        "ttl": ttl_epoch,
    }

    try:
        table.put_item(Item=record)
        logger.info(
            "Audit record written: action_id=%s type=%s account=%s",
            action_id,
            action_type,
            account_id,
        )
    except ClientError as e:
        logger.error(
            "Failed to write audit record: %s", str(e)
        )


def _sanitize_for_dynamodb(obj):
    """Sanitize a dict for DynamoDB storage.

    Removes None values and converts non-serializable types.

    Args:
        obj: Dict to sanitize.

    Returns:
        dict: Sanitized dict safe for DynamoDB.
    """
    if not isinstance(obj, dict):
        return str(obj) if obj is not None else ""

    sanitized = {}
    for key, value in obj.items():
        if value is None:
            continue
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_for_dynamodb(value)
        elif isinstance(value, list):
            sanitized[key] = [
                _sanitize_for_dynamodb(v) if isinstance(v, dict) else str(v)
                for v in value
                if v is not None
            ]
        elif isinstance(value, (int, float)):
            sanitized[key] = str(value)
        else:
            sanitized[key] = str(value)

    return sanitized
