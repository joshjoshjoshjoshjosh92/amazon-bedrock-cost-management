"""Notification Sender Lambda function.

Constructs and sends formatted notification messages for warning, throttle, shutoff,
and restore events. Publishes to team SNS topics and the central FinOps SNS topic.
Notifications include account ID, spend amount, budget limit, and re-enablement
instructions for shutoff events.
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
DEFAULT_FINOPS_TOPIC_ARN = os.environ.get("FINOPS_SNS_TOPIC_ARN", "")
RESTORE_API_ENDPOINT = os.environ.get("RESTORE_API_ENDPOINT", "")

# Clients
sns = boto3.client("sns")


def lambda_handler(event, context):
    """Send formatted notification for a cost governance event.

    Args:
        event: Step Functions input containing event type and notification details.
            Expected fields:
            - action: "warn" | "throttle" | "shutoff" | "restore"
            - account_id: target account
            - team_id: team identifier
            - spend_amount_usd: current spend
            - budget_limit_usd: budget limit
            - spend_pct: spend as percentage of budget
            - team_sns_topic_arn: team notification topic ARN
            - finops_sns_topic_arn: central FinOps topic ARN

        context: Lambda execution context.

    Returns:
        dict: Notification delivery status with message IDs.
    """
    action = event.get("action", "unknown")
    account_id = event.get("account_id", "unknown")
    team_id = event.get("team_id", "unknown")
    spend_amount_usd = event.get("spend_amount_usd", 0)
    budget_limit_usd = event.get("budget_limit_usd", 0)
    spend_pct = event.get("spend_pct", 0)
    team_sns_topic_arn = event.get("team_sns_topic_arn", "")
    finops_sns_topic_arn = event.get("finops_sns_topic_arn", DEFAULT_FINOPS_TOPIC_ARN)

    logger.info(
        "Sending notification: action=%s account=%s team=%s spend=%.2f limit=%.2f",
        action,
        account_id,
        team_id,
        spend_amount_usd,
        budget_limit_usd,
    )

    # Construct the notification message
    subject, message = construct_notification(
        action=action,
        account_id=account_id,
        team_id=team_id,
        spend_amount_usd=spend_amount_usd,
        budget_limit_usd=budget_limit_usd,
        spend_pct=spend_pct,
    )

    results = {
        "action": action,
        "account_id": account_id,
        "team_id": team_id,
        "notifications_sent": [],
        "errors": [],
    }

    # Publish to team SNS topic
    if team_sns_topic_arn:
        team_result = _publish_to_topic(
            topic_arn=team_sns_topic_arn,
            subject=subject,
            message=message,
            target="team",
        )
        if team_result.get("success"):
            results["notifications_sent"].append(
                {"target": "team", "message_id": team_result["message_id"]}
            )
        else:
            results["errors"].append(
                {"target": "team", "error": team_result.get("error", "unknown")}
            )

    # Publish to central FinOps SNS topic
    if finops_sns_topic_arn:
        finops_result = _publish_to_topic(
            topic_arn=finops_sns_topic_arn,
            subject=subject,
            message=message,
            target="finops",
        )
        if finops_result.get("success"):
            results["notifications_sent"].append(
                {"target": "finops", "message_id": finops_result["message_id"]}
            )
        else:
            results["errors"].append(
                {"target": "finops", "error": finops_result.get("error", "unknown")}
            )

    results["status"] = "sent" if results["notifications_sent"] else "failed"

    logger.info(
        "Notification result: action=%s sent=%d errors=%d",
        action,
        len(results["notifications_sent"]),
        len(results["errors"]),
    )

    return results


def construct_notification(action, account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct):
    """Construct formatted notification subject and message body.

    Args:
        action: Event type (warn, throttle, shutoff, restore).
        account_id: Target AWS account ID.
        team_id: Team identifier.
        spend_amount_usd: Current spend amount in USD.
        budget_limit_usd: Budget limit in USD.
        spend_pct: Spend as percentage of budget.

    Returns:
        tuple: (subject, message) strings.
    """
    if action == "warn":
        return _construct_warning_message(
            account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct
        )
    elif action == "throttle":
        return _construct_throttle_message(
            account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct
        )
    elif action == "shutoff":
        return _construct_shutoff_message(
            account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct
        )
    elif action == "restore":
        return _construct_restore_message(
            account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct
        )
    else:
        return _construct_generic_message(
            action, account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct
        )


def _construct_warning_message(account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct):
    """Construct warning threshold notification."""
    subject = f"[Bedrock Cost Warning] Team {team_id} - Account {account_id} at {spend_pct:.1f}% of budget"

    message = (
        f"BEDROCK COST WARNING\n"
        f"{'=' * 50}\n\n"
        f"Team '{team_id}' has reached the warning threshold for Bedrock spend.\n\n"
        f"Details:\n"
        f"  Account ID:     {account_id}\n"
        f"  Current Spend:  ${spend_amount_usd:,.2f}\n"
        f"  Budget Limit:   ${budget_limit_usd:,.2f}\n"
        f"  Usage:          {spend_pct:.1f}% of budget\n\n"
        f"Action Required:\n"
        f"  Review your team's Bedrock usage and consider reducing consumption\n"
        f"  to avoid throttling or shutoff.\n\n"
        f"  If this spend is expected, no action is needed. The system will\n"
        f"  continue monitoring and escalate if higher thresholds are breached.\n"
    )

    return subject, message


def _construct_throttle_message(account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct):
    """Construct throttle action notification."""
    subject = f"[Bedrock Cost THROTTLE] Team {team_id} - Account {account_id} at {spend_pct:.1f}% of budget"

    message = (
        f"BEDROCK COST THROTTLE APPLIED\n"
        f"{'=' * 50}\n\n"
        f"Team '{team_id}' has exceeded the throttle threshold. Provisioned\n"
        f"throughput has been reduced to limit further spend.\n\n"
        f"Details:\n"
        f"  Account ID:     {account_id}\n"
        f"  Current Spend:  ${spend_amount_usd:,.2f}\n"
        f"  Budget Limit:   ${budget_limit_usd:,.2f}\n"
        f"  Usage:          {spend_pct:.1f}% of budget\n\n"
        f"Impact:\n"
        f"  Bedrock provisioned throughput has been reduced. On-demand model\n"
        f"  invocations may experience increased latency or throttling.\n\n"
        f"Action Required:\n"
        f"  Contact your FinOps team to review the budget allocation or\n"
        f"  reduce Bedrock usage immediately to prevent a full shutoff.\n"
    )

    return subject, message


def _construct_shutoff_message(account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct):
    """Construct shutoff action notification with re-enablement instructions."""
    restore_endpoint = RESTORE_API_ENDPOINT or "https://<api-gateway-url>/restore"

    subject = f"[Bedrock SHUTOFF] Team {team_id} - Account {account_id} BUDGET EXCEEDED"

    message = (
        f"BEDROCK ACCESS SHUTOFF\n"
        f"{'=' * 50}\n\n"
        f"CRITICAL: Bedrock access has been REVOKED for account {account_id}.\n\n"
        f"Team '{team_id}' has exceeded the hard budget limit. An IAM deny policy\n"
        f"has been applied to prevent further Bedrock API calls.\n\n"
        f"Details:\n"
        f"  Account ID:     {account_id}\n"
        f"  Current Spend:  ${spend_amount_usd:,.2f}\n"
        f"  Budget Limit:   ${budget_limit_usd:,.2f}\n"
        f"  Usage:          {spend_pct:.1f}% of budget\n\n"
        f"Impact:\n"
        f"  ALL Bedrock API calls (InvokeModel, Converse, InvokeAgent, etc.)\n"
        f"  will be denied for this account until access is restored.\n\n"
        f"Re-enablement Instructions:\n"
        f"  To restore Bedrock access after budget review:\n\n"
        f"  1. An authorized administrator must call the restore endpoint:\n"
        f"     POST {restore_endpoint}/{account_id}\n\n"
        f"  2. Request body must include:\n"
        f"     {{\n"
        f'       "confirmAccountId": "{account_id}",\n'
        f'       "reason": "<justification for restoring access>"\n'
        f"     }}\n\n"
        f"  3. The administrator must have the BedrockCostSentryAdmin IAM role\n"
        f"     and MFA authentication (if required by policy).\n\n"
        f"  4. Contact your FinOps team to request a budget increase or\n"
        f"     discuss usage optimization before restoring access.\n"
    )

    return subject, message


def _construct_restore_message(account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct):
    """Construct access restoration notification."""
    subject = f"[Bedrock RESTORED] Team {team_id} - Account {account_id} access restored"

    message = (
        f"BEDROCK ACCESS RESTORED\n"
        f"{'=' * 50}\n\n"
        f"Bedrock access has been RESTORED for account {account_id}.\n\n"
        f"Details:\n"
        f"  Account ID:     {account_id}\n"
        f"  Team:           {team_id}\n"
        f"  Spend at Restore: ${spend_amount_usd:,.2f}\n"
        f"  Budget Limit:   ${budget_limit_usd:,.2f}\n\n"
        f"Note:\n"
        f"  The budget monitoring system will continue to track spend.\n"
        f"  If the budget limit is reached again, enforcement will be\n"
        f"  re-applied automatically.\n"
    )

    return subject, message


def _construct_generic_message(action, account_id, team_id, spend_amount_usd, budget_limit_usd, spend_pct):
    """Construct a generic notification for unknown action types."""
    subject = f"[Bedrock Cost Alert] Team {team_id} - Account {account_id} - {action}"

    message = (
        f"BEDROCK COST ALERT: {action.upper()}\n"
        f"{'=' * 50}\n\n"
        f"A cost governance event has occurred for team '{team_id}'.\n\n"
        f"Details:\n"
        f"  Action:         {action}\n"
        f"  Account ID:     {account_id}\n"
        f"  Current Spend:  ${spend_amount_usd:,.2f}\n"
        f"  Budget Limit:   ${budget_limit_usd:,.2f}\n"
        f"  Usage:          {spend_pct:.1f}% of budget\n"
    )

    return subject, message


def _publish_to_topic(topic_arn, subject, message, target):
    """Publish a notification message to an SNS topic.

    Args:
        topic_arn: SNS topic ARN.
        subject: Message subject (max 100 chars for SNS).
        message: Message body.
        target: Target identifier for logging (e.g., "team", "finops").

    Returns:
        dict: {success: bool, message_id: str} or {success: bool, error: str}
    """
    # SNS subject has a 100-character limit
    truncated_subject = subject[:100] if len(subject) > 100 else subject

    try:
        response = sns.publish(
            TopicArn=topic_arn,
            Subject=truncated_subject,
            Message=message,
            MessageAttributes={
                "event_type": {
                    "DataType": "String",
                    "StringValue": "bedrock-cost-sentry",
                },
            },
        )
        message_id = response.get("MessageId", "")
        logger.info(
            "Published to %s topic %s: message_id=%s",
            target,
            topic_arn,
            message_id,
        )
        return {"success": True, "message_id": message_id}

    except ClientError as e:
        error_msg = str(e)
        logger.error(
            "Failed to publish to %s topic %s: %s",
            target,
            topic_arn,
            error_msg,
        )
        return {"success": False, "error": error_msg}
