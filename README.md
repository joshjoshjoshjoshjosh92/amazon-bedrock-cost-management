# Amazon Bedrock Cost Tracker

![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)

A progressive, multi-tier cost tracking and governance solution for Amazon Bedrock. Published as an AWS Solutions Library pattern.

## Architecture Overview

This solution provides **five tiers** of cost tracking capability. Each tier builds incrementally on the previous — deploy only what you need, when you need it.

```
Tier 1 (free)     → Cost Explorer + Budgets + Anomaly Detection
Tier 2 (free)     → Application Inference Profiles + Cost Allocation Tags
Tier 3 (deploy)   → CloudWatch Metrics + Dashboards + Alarms
Tier 4 (deploy)   → CUR + Athena Analytics + optional QuickSight
Tier 5 (deploy)   → Proactive Cost Sentry (automated governance)
```

| Tier | Capability | What It Does | Deployment |
|------|-----------|--------------|------------|
| 1 | Cost Explorer + Budgets | Basic spend visibility and budget alerts | Config guide |
| 2 | Inference Profiles + Tags | Per-team/app cost attribution | Config guide |
| 3 | CloudWatch Monitoring | Real-time token metrics, dashboards, alarms | `sam deploy` |
| 4 | CUR Analytics | SQL-queryable cost data, chargeback reports | `sam deploy` |
| 5 | Cost Sentry | Automated budget enforcement (warn → throttle → shutoff) | `sam deploy` |

**Cross-cutting components:**
- Python SDK Wrapper — pip-installable, optional Lambda layer
- Pre-built Athena query library (10 queries for common scenarios)
- Cross-account IAM role templates
- Architecture diagrams and deployment guides

### How the Tiers Work Together

```
┌─────────────────────────────────────────────────────────────────┐
│  Your Application                                                │
│  ┌──────────────────┐                                           │
│  │  SDK Wrapper      │──── tracks tokens + cost per request      │
│  └────────┬─────────┘                                           │
│           │                                                      │
│           ▼                                                      │
│  Amazon Bedrock (via Inference Profile from Tier 2)              │
└─────────────────────────────────────────────────────────────────┘
        │                    │                      │
        ▼                    ▼                      ▼
   ┌─────────┐        ┌──────────┐          ┌───────────┐
   │ Tier 3  │        │  Tier 4  │          │  Tier 5   │
   │ CW Logs │        │   CUR    │          │  Sentry   │
   │ Metrics │        │  Athena  │          │ Step Func │
   │ Alarms  │        │  Glue    │          │ DynamoDB  │
   └─────────┘        └──────────┘          └───────────┘
```

## Prerequisites

- AWS Account (or AWS Organizations for multi-account)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) >= 1.90.0
- Python 3.9 or later
- AWS CLI configured with appropriate credentials
- (Optional) AWS Organizations with delegated administrator for cost management

## Quick Start

### Tier 1 & 2 — Configuration Guides (No Deployment Required)

Follow the step-by-step guides in the `guides/` directory:
- [Tier 1: Cost Explorer Setup](guides/tier1-cost-explorer.md) — Budget alerts, anomaly detection
- [Tier 2: Inference Profiles](guides/tier2-inference-profiles.md) — Per-team cost attribution

### Tier 3 — CloudWatch Monitoring

```bash
sam build --template tiers/tier3-cloudwatch/template.yaml
sam deploy --template tiers/tier3-cloudwatch/template.yaml --guided
```

### Tier 4 — CUR Analytics

```bash
sam build --template tiers/tier4-cur-analytics/template.yaml
sam deploy --template tiers/tier4-cur-analytics/template.yaml --guided
```

### Tier 5 — Cost Sentry

> **Important:** Tier 3 is strongly recommended as a prerequisite for Tier 5. Without Tier 3, enforcement decisions rely on CUR data with up to 24-hour lag.

```bash
sam build --template tiers/tier5-cost-sentry/template.yaml
sam deploy --template tiers/tier5-cost-sentry/template.yaml --guided
```

### Full Stack (All Tiers)

```bash
sam build
sam deploy --guided
```

## SDK Wrapper

The SDK wrapper instruments Bedrock API calls to capture per-request token usage and cost with <50ms overhead.

### Installation

```bash
pip install ./sdk-wrapper
```

Or as a Lambda layer (see `lambda-layer/` for packaging).

### Context Manager Pattern

```python
from bedrock_cost_tracker import BedrockCostTracker, TrackerConfig
import boto3, json

config = TrackerConfig(
    team="ml-platform",
    application="chatbot-v2",
    environment="production",
    custom_tags={"cost_center": "CC-1234"},
    emit_to="cloudwatch",  # "cloudwatch" | "s3" | "both"
)

tracker = BedrockCostTracker(config)
bedrock = boto3.client("bedrock-runtime")

with tracker.track(bedrock) as tracked_client:
    response = tracked_client.invoke_model(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": "Hello!"}],
            "max_tokens": 256
        })
    )
# Tokens, cost, and latency are automatically tracked
```

### Decorator Pattern

```python
@tracker.instrument
def my_bedrock_call(client):
    return client.invoke_model(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        body=json.dumps({"messages": [{"role": "user", "content": "Summarize this."}]})
    )

response = my_bedrock_call(bedrock)
```

### Streaming Support

```python
with tracker.track(bedrock) as tracked_client:
    response = tracked_client.invoke_model_with_response_stream(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": "Write a story."}],
            "max_tokens": 1024
        })
    )
    for event in response["body"]:
        print(event["chunk"]["bytes"].decode(), end="")
    # Token counts captured after stream completes
```

### Converse API

```python
with tracker.track(bedrock) as tracked_client:
    response = tracked_client.converse(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        messages=[{"role": "user", "content": [{"text": "Hello!"}]}]
    )
```

## Cost Estimates

Infrastructure costs per tier (excludes Bedrock inference costs):

| Tier | Low (1K req/day) | Medium (100K req/day) | High (1M req/day) |
|------|-------------------|----------------------|-------------------|
| 1 — Cost Explorer | $0 | $0 | $0 |
| 2 — Inference Profiles | $0 | $0 | $0 |
| 3 — CloudWatch | ~$3/month | ~$15/month | ~$80/month |
| 4 — CUR + Athena | ~$5/month | ~$25/month | ~$120/month |
| 5 — Cost Sentry | ~$8/month | ~$40/month | ~$200/month |
| **Total (all tiers)** | **~$16/month** | **~$80/month** | **~$400/month** |

**Cost breakdown notes:**
- Tier 3: CloudWatch custom metrics ($0.30/metric/month), log storage, dashboard
- Tier 4: S3 storage (Intelligent-Tiering), Glue crawler runs, Athena queries scanned
- Tier 5: DynamoDB on-demand, Step Functions executions, Lambda invocations, API Gateway requests
- QuickSight adds ~$18/month per author if enabled in Tier 4
- Actual costs depend on usage patterns, data retention, and optional features

## Guides and Documentation

| Guide | Description |
|-------|-------------|
| [Tier 1: Cost Explorer](guides/tier1-cost-explorer.md) | Budget alerts and anomaly detection setup |
| [Tier 2: Inference Profiles](guides/tier2-inference-profiles.md) | Tag strategy and Inference Profile creation |
| [Deployment Guide](guides/deployment-guide.md) | IAM permissions, parameters, multi-account deployment |
| [Architecture Diagrams](diagrams/README.md) | Mermaid source files for each tier |

## Cleanup

### Remove Tier 5

```bash
sam delete --stack-name bedrock-cost-sentry
```

### Remove Tier 4

```bash
sam delete --stack-name bedrock-cur-analytics
```

**Note:** Ensure the CUR S3 bucket is empty before deletion, or set `RetainBucket: true` in parameters.

### Remove Tier 3

```bash
sam delete --stack-name bedrock-cloudwatch
```

### Remove All Tiers

```bash
sam delete --stack-name bedrock-cost-tracker
```

## Project Structure

```
bedrock-cost-tracker/
├── template.yaml              # Root SAM template (orchestrator)
├── samconfig.toml             # Example deployment config
├── tiers/
│   ├── tier3-cloudwatch/      # CloudWatch monitoring resources
│   ├── tier4-cur-analytics/   # CUR pipeline and Athena
│   └── tier5-cost-sentry/     # Proactive cost governance
├── sdk-wrapper/               # Python SDK wrapper package
├── lambda-layer/              # Lambda layer packaging
├── functions/                 # Lambda function source code
├── queries/athena/            # Pre-built Athena queries
├── guides/                    # Tier 1 & 2 configuration guides + deployment guide
├── diagrams/                  # Architecture diagram sources (Mermaid)
└── cross-account/             # Cross-account IAM templates
```

## Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for reporting security issues.

## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
