# Deployment Guide

This guide covers IAM permissions, parameter configuration, multi-account deployment, and troubleshooting for Tiers 3–5.

## Table of Contents

- [IAM Permissions by Tier](#iam-permissions-by-tier)
- [Tier 3: CloudWatch Monitoring](#tier-3-cloudwatch-monitoring)
- [Tier 4: CUR Analytics](#tier-4-cur-analytics)
- [Tier 5: Cost Sentry](#tier-5-cost-sentry)
- [Multi-Account Deployment](#multi-account-deployment)
- [Rollover Grace Minutes Tradeoffs](#rollover-grace-minutes-tradeoffs)
- [Troubleshooting](#troubleshooting)

---

## IAM Permissions by Tier

### Permissions Required for Deployment

The deploying principal (user or role running `sam deploy`) needs these permissions:

#### Tier 3 — CloudWatch Monitoring

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "s3:*",
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:PutMetricFilter",
        "logs:DeleteMetricFilter",
        "logs:DescribeLogGroups",
        "cloudwatch:PutDashboard",
        "cloudwatch:DeleteDashboards",
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DeleteAlarms",
        "cloudwatch:DescribeAlarms",
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:Subscribe",
        "sns:SetTopicAttributes",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole"
      ],
      "Resource": "*"
    }
  ]
}
```

#### Tier 4 — CUR Analytics

All Tier 3 permissions plus:

```json
{
  "Effect": "Allow",
  "Action": [
    "cur:PutReportDefinition",
    "cur:DeleteReportDefinition",
    "glue:CreateDatabase",
    "glue:DeleteDatabase",
    "glue:CreateCrawler",
    "glue:DeleteCrawler",
    "glue:StartCrawler",
    "glue:GetCrawler",
    "athena:CreateWorkGroup",
    "athena:DeleteWorkGroup",
    "athena:UpdateWorkGroup",
    "quicksight:CreateDataSource",
    "quicksight:CreateDataSet",
    "quicksight:CreateDashboard",
    "quicksight:DeleteDataSource",
    "quicksight:DeleteDataSet",
    "quicksight:DeleteDashboard"
  ],
  "Resource": "*"
}
```

#### Tier 5 — Cost Sentry

All Tier 3 and 4 permissions plus:

```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:CreateTable",
    "dynamodb:DeleteTable",
    "dynamodb:UpdateTable",
    "dynamodb:DescribeTable",
    "dynamodb:UpdateTimeToLive",
    "states:CreateStateMachine",
    "states:DeleteStateMachine",
    "states:UpdateStateMachine",
    "states:TagResource",
    "lambda:CreateFunction",
    "lambda:DeleteFunction",
    "lambda:UpdateFunctionCode",
    "lambda:UpdateFunctionConfiguration",
    "lambda:AddPermission",
    "lambda:RemovePermission",
    "lambda:GetFunction",
    "apigateway:*",
    "events:PutRule",
    "events:DeleteRule",
    "events:PutTargets",
    "events:RemoveTargets",
    "wafv2:CreateWebACL",
    "wafv2:DeleteWebACL",
    "wafv2:AssociateWebACL",
    "kms:CreateKey",
    "kms:DescribeKey",
    "kms:CreateAlias"
  ],
  "Resource": "*"
}
```

### Permissions Required for Operation (Runtime)

#### SDK Wrapper Runtime Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "cloudwatch:PutMetricData",
    "s3:PutObject",
    "sts:GetCallerIdentity"
  ],
  "Resource": "*",
  "Condition": {
    "StringEquals": {
      "cloudwatch:namespace": "BedrockCostTracker/Invocations"
    }
  }
}
```

#### Cost Sentry Lambda Execution Permissions

The SAM template creates execution roles automatically. Key permissions include:
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query` on budget/spend/audit tables
- `cloudwatch:GetMetricData` for spend evaluation
- `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:SimulatePrincipalPolicy` for enforcement
- `organizations:AttachPolicy`, `organizations:DetachPolicy` for SCP enforcement
- `sns:Publish` for notifications
- `athena:StartQueryExecution`, `athena:GetQueryResults` for reconciliation

---

## Tier 3: CloudWatch Monitoring

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EnvironmentName` | `production` | Environment identifier for resource naming |
| `NotificationEmail` | *(required)* | Email for alarm notifications |
| `ResourcePrefix` | `bedrock-tracker` | Prefix for all resource names |
| `MonitoredModels` | `*` (all) | Comma-separated model IDs to monitor |
| `TokenAlarmThreshold` | `1000000` | Token count threshold for alarms (1M) |
| `AlarmEvaluationPeriod` | `300` | Alarm evaluation period in seconds (5 min) |

### Deployment

```bash
sam build --template tiers/tier3-cloudwatch/template.yaml

sam deploy --template tiers/tier3-cloudwatch/template.yaml \
  --stack-name bedrock-cloudwatch \
  --parameter-overrides \
    EnvironmentName=production \
    NotificationEmail=ops@example.com \
    TokenAlarmThreshold=500000 \
  --capabilities CAPABILITY_IAM
```

### Post-Deployment

1. Confirm the SNS subscription email
2. Enable Bedrock model invocation logging in the Bedrock console (Settings → Model invocation logging → Enable, select the CloudWatch log group created by the stack)

---

## Tier 4: CUR Analytics

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `OrganizationId` | *(required for multi-account)* | AWS Organizations ID |
| `CURBucketName` | *(auto-generated)* | S3 bucket for CUR data |
| `EnableQuickSight` | `false` | Enable QuickSight dashboard |
| `DataRetentionMonths` | `13` | Months to retain CUR data |
| `GlueCrawlerSchedule` | `cron(0 1 * * ? *)` | Glue crawler schedule (daily 1 AM UTC) |

### Deployment

```bash
sam build --template tiers/tier4-cur-analytics/template.yaml

sam deploy --template tiers/tier4-cur-analytics/template.yaml \
  --stack-name bedrock-cur-analytics \
  --parameter-overrides \
    OrganizationId=o-abc123def4 \
    DataRetentionMonths=13 \
    EnableQuickSight=false \
  --capabilities CAPABILITY_IAM
```

### Post-Deployment

1. Wait 24 hours for the first CUR delivery
2. After delivery, the Glue crawler runs automatically and populates the Athena database
3. Test with a sample query:

```bash
aws athena start-query-execution \
  --query-string "SELECT * FROM bedrock_usage LIMIT 10" \
  --work-group "bedrock-cost-tracker"
```

---

## Tier 5: Cost Sentry

### Prerequisites

> **Tier 3 is required for real-time enforcement.** Without Tier 3 CloudWatch metrics, the Cost Sentry relies on CUR data with up to 24-hour lag. During that lag, a team could spend their entire budget with no enforcement. Tier 3 is optional only if you use `notify-only` enforcement mode.

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ResourcePrefix` | `bedrock-sentry` | Prefix for DynamoDB tables and resources |
| `EvaluationSchedule` | `rate(5 minutes)` | How often the Sentry evaluates budgets |
| `KmsKeyArn` | *(empty)* | Customer-managed KMS key ARN (optional) |
| `EncryptionMode` | `aws-managed` | `aws-managed` or `customer-cmk` |
| `RequireMFAForRestore` | `true` | Require MFA for restore endpoint |
| `NotificationEmail` | *(required)* | FinOps team email for alerts |

### Deployment

```bash
sam build --template tiers/tier5-cost-sentry/template.yaml

sam deploy --template tiers/tier5-cost-sentry/template.yaml \
  --stack-name bedrock-cost-sentry \
  --parameter-overrides \
    ResourcePrefix=bedrock-sentry \
    EvaluationSchedule="rate(5 minutes)" \
    RequireMFAForRestore=true \
    NotificationEmail=finops@example.com \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM
```

### Post-Deployment

1. Confirm the SNS subscription email
2. Create your first budget via the API:

```bash
API_URL=$(aws cloudformation describe-stacks \
  --stack-name bedrock-cost-sentry \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)

curl -X POST "$API_URL/budgets" \
  --aws-sigv4 "aws:amz:us-east-1:execute-api" \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": "ml-platform",
    "budget_name": "ML Platform Monthly",
    "period": "monthly",
    "hard_limit_usd": 5000,
    "warning_threshold_pct": 80,
    "throttle_threshold_pct": 90,
    "enforcement_mode": "shutoff",
    "enforcement_scope": "account-wide",
    "account_ids": ["123456789012"],
    "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:ml-platform-alerts"
  }'
```

---

## Multi-Account Deployment

### Architecture

```
Management Account (or Delegated Admin)
├── Tier 4: CUR Pipeline (aggregates all accounts)
├── Tier 5: Cost Sentry (central governance)
└── Cross-account role (reads CUR data)

Member Account A
├── Tier 3: CloudWatch Monitoring
├── SDK Wrapper (in application code)
└── Cross-account role (grants read access to central S3)

Member Account B
├── Tier 3: CloudWatch Monitoring
├── SDK Wrapper (in application code)
└── Cross-account role (grants read access to central S3)
```

### Step 1: Deploy Central Infrastructure (Management Account)

```bash
# Deploy Tier 4 with Organization ID
sam deploy --template tiers/tier4-cur-analytics/template.yaml \
  --stack-name bedrock-cur-analytics \
  --parameter-overrides OrganizationId=o-abc123def4

# Deploy Tier 5
sam deploy --template tiers/tier5-cost-sentry/template.yaml \
  --stack-name bedrock-cost-sentry \
  --parameter-overrides NotificationEmail=finops@example.com
```

### Step 2: Deploy Cross-Account Roles (Each Member Account)

```bash
aws cloudformation deploy \
  --template-file cross-account/iam-role-template.yaml \
  --stack-name bedrock-cost-tracker-role \
  --parameter-overrides \
    OrganizationId=o-abc123def4 \
    CentralBucketArn=arn:aws:s3:::bedrock-cur-data-ACCOUNT_ID \
  --capabilities CAPABILITY_NAMED_IAM
```

### Step 3: Deploy Tier 3 (Each Member Account)

```bash
sam deploy --template tiers/tier3-cloudwatch/template.yaml \
  --stack-name bedrock-cloudwatch \
  --parameter-overrides NotificationEmail=team@example.com
```

### Step 4: Deploy Require-Inference-Profile SCP (Optional)

If using tag-scoped enforcement, deploy the SCP from the management account:

```bash
aws cloudformation deploy \
  --template-file cross-account/require-inference-profile-scp.yaml \
  --stack-name require-inference-profile-scp \
  --parameter-overrides TargetOUs=ou-abc1-23456789
```

---

## Rollover Grace Minutes Tradeoffs

The `rollover_grace_minutes` parameter controls how long the Cost Sentry waits before enforcing budgets at the start of a new period.

| Value | Behavior | Risk |
|-------|----------|------|
| `0` | Immediate enforcement in new period | False enforcement from stale CloudWatch metrics still reporting previous period's spend |
| `15` | Short grace period | Low risk of false enforcement; small window of uncontrolled spend |
| `60` (default) | Standard grace period | Balanced — allows CW metrics to flush; 1 hour of uncontrolled spend at period boundary |
| `1440` (max) | Full day grace | No false enforcement risk; 24 hours of uncontrolled spend at period start |

**Recommendation:** Use the default (60 minutes) unless you have specific requirements. If your teams have very high burst potential at period boundaries, consider 0 with `notify-only` mode for the first period to validate behavior.

---

## Troubleshooting

### Tier 3 Issues

**No metrics appearing in CloudWatch:**
1. Verify Bedrock model invocation logging is enabled (Bedrock console → Settings)
2. Check the log group has the correct name (matches the SAM template output)
3. Verify the metric filter patterns match the log format — check CloudWatch Logs Insights:
   ```
   fields @timestamp, @message
   | filter @message like /ModelInvocationLog/
   | limit 5
   ```

**Alarms stuck in INSUFFICIENT_DATA:**
- The alarm needs at least one data point. Make a Bedrock API call and wait for the evaluation period to pass.

### Tier 4 Issues

**CUR data not appearing in Athena:**
1. CUR delivery takes up to 24 hours after initial setup
2. Check the S3 bucket for delivered files
3. Verify the Glue crawler ran successfully: `aws glue get-crawler --name bedrock-cur-crawler`
4. Manually trigger the crawler: `aws glue start-crawler --name bedrock-cur-crawler`

**Athena query returns empty results:**
- Ensure the query includes the Bedrock filter: `WHERE line_item_product_code = 'AmazonBedrock'`
- Check if cost allocation tags are activated (Billing console → Cost allocation tags)
- Verify the date range covers a period with actual Bedrock usage

### Tier 5 Issues

**Enforcement not triggering:**
1. Verify Tier 3 is deployed (required for real-time enforcement)
2. Check the Step Functions execution history for errors
3. Verify the budget exists: `GET /budgets/{team_id}`
4. Check the evaluation lock table for stale locks
5. Review CloudWatch Logs for the spend_evaluator Lambda

**Restore endpoint returns 403:**
1. Verify your IAM role has the `BedrockCostSentryAdmin` policy attached
2. If `RequireMFAForRestore=true`, ensure your session has MFA: `aws sts get-session-token --serial-number arn:aws:iam::ACCOUNT:mfa/USER --token-code 123456`
3. Check the API Gateway resource policy allows your source IP

**DynamoDB throttling:**
- Tables use on-demand capacity by default. If you see throttling, check for hot partitions in the spend-history table.
- The evaluation-locks table may see bursts during fast-path triggers — this is normal and self-resolving.

**Reconciliation drift alerts:**
- A drift alert means CloudWatch-derived spend differs from CUR ground truth by more than 5% or $500.
- Common causes: delayed CUR delivery, CloudWatch metric gaps, timezone boundary effects.
- The reconciliation Lambda auto-corrects the drift — no manual action needed unless drift is persistent.

### General Issues

**SAM deploy fails with "Unable to upload artifact":**
- Ensure you have an S3 bucket for SAM artifacts: `sam deploy --guided` creates one automatically.

**Stack deletion fails:**
- S3 buckets with objects cannot be deleted. Empty the bucket first or set `RetainBucket: true`.
- DynamoDB tables with deletion protection require manual removal of the protection flag.

**SDK Wrapper not emitting metrics:**
- Check IAM permissions: the execution role needs `cloudwatch:PutMetricData`
- Verify the `emit_to` config matches your setup (`cloudwatch`, `s3`, or `both`)
- Check for circuit breaker activation in logs (5 consecutive failures triggers 60s cooldown)
