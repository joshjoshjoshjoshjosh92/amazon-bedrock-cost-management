#!/bin/bash
set -e

# ─── Amazon Bedrock Cost Tracker — One-Click Deploy ──────────────────────────
#
# Deploys the Bedrock Cost Tracker with configurable tiers.
# Default: Tier 3 (CloudWatch monitoring)
#
# Usage:
#   ./deploy.sh --email team@company.com              # Tier 3 only (default)
#   ./deploy.sh --email team@company.com --tier4      # Tiers 3 + 4
#   ./deploy.sh --email team@company.com --all        # All tiers
#   ./deploy.sh --destroy                             # Remove everything
#
# Options:
#   --email <addr>     Notification email (required for deploy)
#   --tier4            Enable Tier 4 (CUR Analytics)
#   --tier5            Enable Tier 5 (Cost Sentry)
#   --all              Enable all tiers
#   --org-id <id>      AWS Organizations ID (for multi-account)
#   --region <region>  AWS region (default: from AWS CLI config)
#   --prefix <prefix>  Resource prefix (default: bct)
#   --destroy          Remove all deployed stacks
#   --skip-wrapper     Skip SDK wrapper installation
# ──────────────────────────────────────────────────────────────────────────────

EMAIL=""
ENABLE_TIER4="false"
ENABLE_TIER5="false"
ORG_ID=""
REGION="${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo 'us-east-1')}"
PREFIX="bct"
DESTROY=false
SKIP_WRAPPER=false
STACK_NAME="bedrock-cost-tracker"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --email) EMAIL="$2"; shift 2 ;;
    --tier4) ENABLE_TIER4="true"; shift ;;
    --tier5) ENABLE_TIER5="true"; shift ;;
    --all) ENABLE_TIER4="true"; ENABLE_TIER5="true"; shift ;;
    --org-id) ORG_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --destroy) DESTROY=true; shift ;;
    --skip-wrapper) SKIP_WRAPPER=true; shift ;;
    *) echo "Unknown option: $1"; echo "Run ./deploy.sh --help for usage"; exit 1 ;;
  esac
done

# ─── Destroy Mode ─────────────────────────────────────────────────────────────
if [ "$DESTROY" = true ]; then
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Removing Bedrock Cost Tracker                              ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Stack: $STACK_NAME"
  echo "  Region: $REGION"
  echo ""
  read -p "  Are you sure? (y/N) " -n 1 -r
  echo ""
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    sam delete --stack-name "$STACK_NAME" --region "$REGION" --no-prompts
    echo ""
    echo "  ✓ Stack deleted"
  else
    echo "  Cancelled."
  fi
  exit 0
fi

# ─── Validate Inputs ──────────────────────────────────────────────────────────
if [ -z "$EMAIL" ]; then
  echo "Error: --email is required"
  echo ""
  echo "Usage: ./deploy.sh --email team@company.com [--tier4] [--tier5] [--all]"
  exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Amazon Bedrock Cost Tracker                                ║"
echo "║  One-Click Deployment                                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Region:       $REGION"
echo "  Prefix:       $PREFIX"
echo "  Email:        $EMAIL"
echo "  Tier 3:       ✓ (always enabled)"
echo "  Tier 4 (CUR): $ENABLE_TIER4"
echo "  Tier 5 (Sentry): $ENABLE_TIER5"
if [ -n "$ORG_ID" ]; then
  echo "  Org ID:       $ORG_ID"
fi
echo ""

# ─── Step 1: Build ────────────────────────────────────────────────────────────
echo "── Step 1/4: Building Lambda functions ───────────────────────────────"
sam build --use-container 2>&1 | tail -5 || sam build 2>&1 | tail -5
echo "  ✓ Build complete"
echo ""

# ─── Step 2: Deploy ───────────────────────────────────────────────────────────
echo "── Step 2/5: Deploying to AWS ────────────────────────────────────────"

# Ensure the Bedrock logging role exists (created outside CloudFormation to avoid conflicts)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME="${PREFIX}-bedrock-logging-role"
LOG_GROUP_NAME="/aws/bedrock/${PREFIX}-model-invocations"

if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "  Creating Bedrock logging IAM role..."
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"bedrock.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT_ID\"},\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:bedrock:$REGION:$ACCOUNT_ID:*\"}}}]}" \
    --description "Allows Bedrock to write model invocation logs to CloudWatch" >/dev/null 2>&1
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name bedrock-logging-policy \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:$LOG_GROUP_NAME:*\"}]}" >/dev/null 2>&1
  echo "  ✓ Logging role created: $ROLE_NAME"
  sleep 10
else
  echo "  ✓ Logging role exists: $ROLE_NAME"
fi

echo "  Deploying stack: $STACK_NAME"
echo "  (This may take 3-5 minutes on first deploy)"
echo ""

PARAMS="NotificationEmail=$EMAIL ResourcePrefix=$PREFIX EnableTier3=true EnableTier4=$ENABLE_TIER4 EnableTier5=$ENABLE_TIER5"
if [ -n "$ORG_ID" ]; then
  PARAMS="$PARAMS OrganizationId=$ORG_ID"
fi

sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND CAPABILITY_NAMED_IAM \
  --parameter-overrides $PARAMS \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  2>&1 | grep -E "✓|✗|Successfully|Error|CREATE_COMPLETE|UPDATE_COMPLETE" || true

echo ""
echo "  ✓ Deployment complete"
echo ""

# ─── Step 3: Enable Bedrock Model Invocation Logging ─────────────────────────
echo "── Step 3/5: Enabling Bedrock model invocation logging ───────────────────"

LOG_GROUP=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query "Stacks[0].Outputs[?contains(OutputKey,'LogGroupName')].OutputValue" --output text 2>/dev/null || echo "")
LOGGING_ROLE=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query "Stacks[0].Outputs[?contains(OutputKey,'LoggingRoleArn')].OutputValue" --output text 2>/dev/null || echo "")

if [ -n "$LOG_GROUP" ] && [ -n "$LOGGING_ROLE" ]; then
  aws bedrock put-model-invocation-logging-configuration --region "$REGION" \
    --logging-config "{
      \"cloudWatchConfig\": {
        \"logGroupName\": \"$LOG_GROUP\",
        \"roleArn\": \"$LOGGING_ROLE\"
      },
      \"textDataDeliveryEnabled\": true,
      \"imageDataDeliveryEnabled\": false,
      \"embeddingDataDeliveryEnabled\": false
    }" 2>/dev/null && echo "  ✓ Bedrock invocation logging enabled → $LOG_GROUP" || echo "  ⚠ Could not enable logging (may need manual setup)"
else
  echo "  ⚠ Could not determine log group or role ARN. Enable logging manually in Bedrock console."
fi
echo ""

# ─── Step 4: Install SDK Wrapper ──────────────────────────────────────────────
if [ "$SKIP_WRAPPER" = false ]; then
  echo "── Step 4/5: Installing SDK wrapper ────────────────────────────────────"
  pip install ./sdk-wrapper --quiet 2>/dev/null || pip install ./sdk-wrapper
  echo "  ✓ SDK wrapper installed (bedrock_cost_tracker)"
  echo ""
else
  echo "── Step 4/5: Skipping SDK wrapper (--skip-wrapper) ─────────────────────"
  echo ""
fi

# ─── Step 5: Verify ──────────────────────────────────────────────────────────
echo "── Step 5/5: Verifying deployment ────────────────────────────────────────"

# Check stack status
STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "NOT_FOUND")

if [[ "$STACK_STATUS" == *"COMPLETE"* ]]; then
  echo "  ✓ Stack status: $STACK_STATUS"
else
  echo "  ⚠ Stack status: $STACK_STATUS (check CloudFormation console for details)"
fi
echo ""

# ─── Done ─────────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅ Deployment Complete!                                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  What's deployed:"
echo "    ✓ Tier 3: CloudWatch metrics, dashboards, and alarms"
if [ "$ENABLE_TIER4" = "true" ]; then
  echo "    ✓ Tier 4: CUR pipeline + Athena analytics"
fi
if [ "$ENABLE_TIER5" = "true" ]; then
  echo "    ✓ Tier 5: Cost Sentry (warn → throttle → shutoff)"
fi
echo ""
echo "  Next steps:"
echo "    1. Check your email ($EMAIL) to confirm SNS subscription"
echo "    2. Follow guides/tier1-cost-explorer.md for budget setup"
echo "    3. Follow guides/tier2-inference-profiles.md for per-team attribution"
echo "    4. Instrument your code with the SDK wrapper:"
echo ""
echo "       from bedrock_cost_tracker import BedrockCostTracker, TrackerConfig"
echo "       tracker = BedrockCostTracker(TrackerConfig(team='my-team', emit_to='cloudwatch'))"
echo ""
echo "  Docs:     README.md"
echo "  Guides:   guides/"
echo "  Cleanup:  ./deploy.sh --destroy"
echo ""
