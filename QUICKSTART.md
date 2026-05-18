# Quick Start — One-Click Deployment

Deploy Bedrock Cost Tracker in under 5 minutes. Start with Tier 3 (CloudWatch monitoring) — add Tiers 4 and 5 when you're ready.

## Prerequisites

- AWS account with Amazon Bedrock enabled
- AWS CLI configured (`aws configure`)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed
- Python 3.9+ (for SDK wrapper)

## One-Click Deploy

```bash
git clone https://github.com/joshjoshjoshjoshjosh92/amazon-bedrock-cost-management.git
cd amazon-bedrock-cost-management
./deploy.sh --email your-team@company.com
```

That's it. The script:
1. Builds all Lambda functions
2. Deploys Tier 3 (CloudWatch monitoring) by default
3. Sets up budget alerts and anomaly detection
4. Installs the SDK wrapper
5. Prints your dashboard URL and next steps

## What You Get (Tier 3 — Default)

| Component | What It Does |
|-----------|-------------|
| **CloudWatch Dashboard** | Real-time Bedrock token usage and cost per team/app |
| **Budget Alarms** | Alerts when spend exceeds thresholds (50%, 80%, 100%) |
| **Custom Metrics** | Input/output tokens, latency, cost per invocation |
| **SDK Wrapper** | Drop-in Python wrapper that tracks cost with <50ms overhead |

## Progressive Tiers

Deploy additional tiers as your needs grow:

```bash
# Add CUR analytics (SQL-queryable cost data)
./deploy.sh --email your-team@company.com --tier4

# Add Cost Sentry (automated budget enforcement)
./deploy.sh --email your-team@company.com --tier4 --tier5

# Deploy everything
./deploy.sh --email your-team@company.com --all
```

| Tier | What It Adds | Monthly Cost |
|------|-------------|-------------|
| 1 & 2 | Cost Explorer + Inference Profiles (config guides only) | $0 |
| 3 | CloudWatch metrics + dashboards + alarms | ~$3–$80 |
| 4 | CUR + Athena + optional QuickSight | ~$5–$120 |
| 5 | Cost Sentry (warn → throttle → shutoff) | ~$8–$200 |

## Using the SDK Wrapper

After deployment, instrument your Bedrock calls:

```python
from bedrock_cost_tracker import BedrockCostTracker, TrackerConfig
import boto3

tracker = BedrockCostTracker(TrackerConfig(
    team="my-team",
    application="my-app",
    emit_to="cloudwatch",
))

bedrock = boto3.client("bedrock-runtime")

with tracker.track(bedrock) as client:
    response = client.converse(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        messages=[{"role": "user", "content": [{"text": "Hello!"}]}]
    )
# Cost automatically tracked in CloudWatch
```

## Multi-Account (Organizations)

For central visibility across accounts:

```bash
# Deploy in management/delegated admin account
./deploy.sh --email finops@company.com --all --org-id o-abc123def4

# Deploy cross-account role in each member account
aws cloudformation deploy \
  --template-file cross-account/iam-role-template.yaml \
  --stack-name bedrock-cost-tracker-role \
  --parameter-overrides \
    OrganizationId=o-abc123def4 \
    CentralBucketArn=arn:aws:s3:::bct-cur-bucket \
    ManagementAccountId=123456789012
```

## Cost

The tracker itself costs less than a cup of coffee:

| Volume | Tier 3 | All Tiers |
|--------|--------|-----------|
| 1K requests/day | ~$3/month | ~$16/month |
| 100K requests/day | ~$15/month | ~$80/month |
| 1M requests/day | ~$80/month | ~$400/month |

## Cleanup

```bash
# Remove specific tier
sam delete --stack-name bedrock-cost-tracker

# Or remove everything
./deploy.sh --destroy
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No metrics appearing" | Ensure you're using the SDK wrapper or Inference Profiles |
| "SAM build fails" | Run `pip install -r requirements.txt` in each function directory |
| "Permission denied on deploy" | Your IAM user needs `cloudformation:*`, `lambda:*`, `iam:*` |
| "Tier 5 not enforcing" | Deploy the SCP: `cross-account/require-inference-profile-scp.yaml` |
