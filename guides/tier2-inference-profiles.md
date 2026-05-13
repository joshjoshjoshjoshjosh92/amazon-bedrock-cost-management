# Tier 2: Application Inference Profiles and Cost Allocation Tags

This guide walks you through setting up per-team and per-application cost attribution for Amazon Bedrock using Application Inference Profiles and cost allocation tags. No infrastructure deployment is required.

**Time to complete:** ~30 minutes

## Prerequisites

- AWS account with Bedrock enabled
- IAM permissions: `bedrock:CreateInferenceProfile`, `bedrock:TagResource`, `bedrock:GetFoundationModel`, `ce:UpdateCostAllocationTagsStatus`
- Billing console access (for tag activation)
- Foundation models enabled in your account

---

## Step 1: Enable a Foundation Model

Before creating an Inference Profile, ensure the model you want to use is enabled in your account.

### Console Steps

1. Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/home)
2. In the left navigation, click **Model access**
3. Click **Manage model access**
4. Select the models your teams need (e.g., Claude 3 Sonnet, Claude 3 Haiku)
5. Click **Save changes**
6. Wait for the status to show **Access granted** (usually immediate for on-demand models)

### CLI Alternative

```bash
# List available foundation models
aws bedrock list-foundation-models \
  --query "modelSummaries[?contains(modelId, 'claude')].{Id:modelId,Name:modelName,Status:modelLifecycle.status}" \
  --output table
```

---

## Step 2: Define a Tag Strategy

A consistent tagging strategy is essential for cost attribution and enables tag-scoped enforcement in Tier 5.

### Recommended Tags

| Tag Key | Purpose | Example Values |
|---------|---------|---------------|
| `Application` | Identifies the workload | `chatbot-v2`, `doc-summarizer`, `code-assistant` |
| `Environment` | Deployment stage | `production`, `staging`, `development` |
| `Team` | Owning team | `ml-platform`, `customer-support`, `engineering` |
| `CostCenter` | Finance allocation code | `CC-1234`, `CC-5678` |

### Naming Conventions for Tag-Scoped Enforcement

If you plan to use Tier 5 tag-scoped enforcement (shutoff per team rather than per account), adopt this naming convention for Inference Profiles:

```
{team}-{application}-{model-short-name}
```

**Examples:**
- `ml-platform-chatbot-claude-sonnet`
- `customer-support-summarizer-claude-haiku`
- `engineering-code-assist-claude-sonnet`

This convention enables wildcard-based enforcement policies. For example, shutting off the `ml-platform` team uses the pattern:

```
arn:aws:bedrock:*:ACCOUNT_ID:application-inference-profile/ml-platform-*
```

> **Important:** Consistent naming is a prerequisite for tag-scoped enforcement in Tier 5. Decide on your convention now, even if you don't plan to deploy Tier 5 immediately.

---

## Step 3: Create an Application Inference Profile with Tags

Application Inference Profiles let you route Bedrock calls through a named, tagged endpoint for cost attribution.

### Console Steps

1. Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/home)
2. In the left navigation, under **Assessment & deployment**, click **Application Inference Profiles**
3. Click **Create application inference profile**
4. Configure the profile:
   - **Inference profile name:** Use your naming convention (e.g., `ml-platform-chatbot-claude-sonnet`)
   - **Select model:** Choose the foundation model (e.g., `anthropic.claude-3-sonnet-20240229-v1:0`)
5. Under **Tags**, add your cost allocation tags:
   - `Application` = `chatbot-v2`
   - `Environment` = `production`
   - `Team` = `ml-platform`
   - `CostCenter` = `CC-1234`
6. Click **Create inference profile**
7. Note the **Inference Profile ARN** — you'll use this in your application code

### CLI Alternative

```bash
aws bedrock create-inference-profile \
  --inference-profile-name "ml-platform-chatbot-claude-sonnet" \
  --model-source '{"copyFrom": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"}' \
  --tags '[
    {"key": "Application", "value": "chatbot-v2"},
    {"key": "Environment", "value": "production"},
    {"key": "Team", "value": "ml-platform"},
    {"key": "CostCenter", "value": "CC-1234"}
  ]'
```

---

## Step 4: Update Application Code to Use the Inference Profile ARN

Replace direct model ID references with the Inference Profile ARN in your application code.

### Python (boto3) Example

**Before (direct model invocation):**

```python
import boto3
import json

bedrock = boto3.client("bedrock-runtime")

response = bedrock.invoke_model(
    modelId="anthropic.claude-3-sonnet-20240229-v1:0",
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 256
    })
)
```

**After (using Inference Profile ARN):**

```python
import boto3
import json

bedrock = boto3.client("bedrock-runtime")

# Use the Inference Profile ARN instead of the model ID
INFERENCE_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:"
    "application-inference-profile/ml-platform-chatbot-claude-sonnet"
)

response = bedrock.invoke_model(
    modelId=INFERENCE_PROFILE_ARN,
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 256
    })
)
```

**With the SDK Wrapper (Tier 3+):**

```python
from bedrock_cost_tracker import BedrockCostTracker, TrackerConfig

config = TrackerConfig(
    team="ml-platform",
    application="chatbot-v2",
    environment="production",
)

tracker = BedrockCostTracker(config)

with tracker.track(bedrock) as tracked_client:
    response = tracked_client.invoke_model(
        modelId=INFERENCE_PROFILE_ARN,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": "Hello!"}],
            "max_tokens": 256
        })
    )
# Cost is automatically tracked and attributed to ml-platform/chatbot-v2
```

> **Tip:** Store the Inference Profile ARN in an environment variable or SSM Parameter Store rather than hardcoding it. This makes it easy to rotate profiles or switch models without code changes.

---

## Step 5: Activate Cost Allocation Tags in the Billing Console

Tags on Inference Profiles only appear in Cost Explorer and CUR after you activate them as cost allocation tags.

### Console Steps

1. Open the [AWS Billing console](https://console.aws.amazon.com/billing/home)
2. In the left navigation, click **Cost allocation tags**
3. Click the **User-defined cost allocation tags** tab
4. Search for your tags: `Application`, `Environment`, `Team`, `CostCenter`
5. Select each tag and click **Activate**
6. Wait up to 24 hours for tags to appear in Cost Explorer and CUR data

### CLI Alternative

```bash
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status '[
    {"TagKey": "Application", "Status": "Active"},
    {"TagKey": "Environment", "Status": "Active"},
    {"TagKey": "Team", "Status": "Active"},
    {"TagKey": "CostCenter", "Status": "Active"}
  ]'
```

> **Note:** Tag activation is account-level. In an AWS Organizations environment, activate tags in the management account (or delegated billing account) for them to appear in consolidated billing reports.

---

## Step 6: Naming Conventions for Tag-Scoped Enforcement

If you plan to use Tier 5 Cost Sentry with tag-scoped enforcement, follow these conventions:

### Profile Naming Pattern

```
{team}-{app}-{model-short-name}
```

| Team | Application | Model | Profile Name |
|------|-------------|-------|-------------|
| ml-platform | chatbot | Claude Sonnet | `ml-platform-chatbot-claude-sonnet` |
| ml-platform | summarizer | Claude Haiku | `ml-platform-summarizer-claude-haiku` |
| customer-support | agent | Claude Sonnet | `customer-support-agent-claude-sonnet` |
| engineering | code-review | Claude Sonnet | `engineering-code-review-claude-sonnet` |

### Enforcement Wildcard Patterns

Tier 5 uses ARN patterns to scope enforcement to specific teams:

| Scope | ARN Pattern |
|-------|-------------|
| Entire team | `arn:aws:bedrock:*:ACCOUNT:application-inference-profile/{team}-*` |
| Team + app | `arn:aws:bedrock:*:ACCOUNT:application-inference-profile/{team}-{app}-*` |
| Specific profile | `arn:aws:bedrock:*:ACCOUNT:application-inference-profile/{team}-{app}-{model}` |

### Complementary SCP (Recommended)

To prevent direct model invocations that bypass tag-scoped enforcement, deploy the require-inference-profile SCP from `cross-account/require-inference-profile-scp.yaml`. This ensures all Bedrock calls go through a tagged Inference Profile.

---

## What You Get

After completing Tier 2, you have:

| Capability | Latency | Cost |
|-----------|---------|------|
| Per-team cost attribution in Cost Explorer | 24-48 hours | $0 |
| Per-application cost breakdown | 24-48 hours | $0 |
| Tag-based filtering in AWS Budgets | ~4 hours | $0 |
| Foundation for tag-scoped enforcement (Tier 5) | — | $0 |

## Limitations

- Cost allocation tags take up to 24 hours to appear in reports after activation
- Tags only apply to new usage — historical data before tag activation is untagged
- Tag-scoped enforcement requires consistent naming discipline across all teams
- Direct model invocations (without Inference Profile) bypass tag attribution unless the complementary SCP is deployed

## Next Steps

- Tier 3: CloudWatch Monitoring — Deploy real-time token-level metrics and dashboards
- [Deployment Guide](../guides/deployment-guide.md) — Full deployment instructions for Tiers 3-5
- Tier 5: Cost Sentry — Automated enforcement using the tag patterns defined here
