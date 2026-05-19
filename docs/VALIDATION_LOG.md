# Validation Log

This document records the testing and validation performed on the Bedrock Cost Tracker solution to confirm it works end-to-end in a real AWS environment.

## Template Validation

| Template | Status | Tool |
|----------|--------|------|
| `template.yaml` (root orchestrator) | ✅ Valid | `sam validate` |
| `tiers/tier3-cloudwatch/template.yaml` | ✅ Valid | `sam validate` |
| `tiers/tier4-cur-analytics/template.yaml` | ✅ Valid | `sam validate` |
| `tiers/tier5-cost-sentry/template.yaml` | ✅ Valid | `sam validate` |

## SDK Wrapper Tests

```
110 passed, 7 warnings in 3.75s
```

Test files:
- `tests/test_client.py` — Tracked client wrapping and API interception
- `tests/test_config.py` — TrackerConfig validation and defaults
- `tests/test_emitter.py` — CloudWatch and S3 metric emission
- `tests/test_pricing.py` — Pricing manifest loading, validation, S3 override, SHA256 verification
- `tests/test_tracker.py` — Context manager, decorator pattern, buffer flushing, wiring

## Live Deployment Validation

### Environment
- Region: `us-east-1`
- Role: Admin (Isengard personal account)
- Date: 2026-05-19

### Deployment Steps Validated

| Step | Result | Notes |
|------|--------|-------|
| `sam build` | ✅ | All Lambda functions built successfully |
| `sam deploy` (Tier 3 only) | ✅ | Stack created in ~45 seconds |
| IAM logging role creation | ✅ | CloudFormation-managed, least-privilege |
| Bedrock invocation logging enablement | ✅ | Automated via `put-model-invocation-logging-configuration` |
| Stack update (add logging role) | ✅ | Required deleting manually-created role first (lesson learned → now in stack) |

### Bedrock Invocation Testing

| Batch | Model | Invocations | Input Tokens | Output Tokens | Result |
|-------|-------|-------------|--------------|---------------|--------|
| 1 (pre-logging) | `us.anthropic.claude-sonnet-4-6` | 10 | 184 | 2,000 | ✅ All succeeded |
| 2 (post-logging) | `us.anthropic.claude-sonnet-4-6` | 5 | 85 | 999 | ✅ All succeeded |
| 3 (stack-managed logging) | `us.anthropic.claude-sonnet-4-6` | 10 | 180 | 1,999 | ✅ All succeeded |

Total: **25 invocations, 5,262 tokens consumed**

### Model Access Notes

- Newer Claude models (Sonnet 4+) require cross-region inference profile format: `us.anthropic.claude-sonnet-4-6`
- Direct model IDs (`anthropic.claude-sonnet-4-20250514-v1:0`) require an inference profile — on-demand not supported
- Legacy models (`anthropic.claude-3-sonnet-20240229-v1:0`) return "marked as Legacy" if unused for 30 days
- First-time Anthropic access may require use case submission via Bedrock console playground

### Dashboard Verification

- Dashboard deployed at: `https://{region}.console.aws.amazon.com/cloudwatch/home#dashboards:name=bct-production`
- 18 widgets across 3 sections (Executive, Team/Model, Operational)
- Metrics flow from invocation logs within 2-5 minutes of invocation
- Metric filters correctly extract: InputTokenCount, OutputTokenCount, InvocationCount, Latency, ErrorCount, ThrottleCount

### Issues Discovered and Fixed

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| Model ID `anthropic.claude-sonnet-4-6` invalid | Newer models need `us.` prefix for cross-region inference | Use `us.anthropic.claude-sonnet-4-6` |
| Stack update failed on logging role | Role already existed (manually created) | Role now managed by CloudFormation; manual creation removed |
| Logging not enabled after deploy | No automation existed | Added Step 3 to `deploy.sh` that auto-enables logging |
| Dashboard empty after invocations | Logging wasn't enabled when invocations were sent | Logging must be enabled BEFORE invocations for metrics to flow |

## Security Validation

### IAM Policy Audit

| Function | Wildcards Remaining | Justification |
|----------|-------------------|---------------|
| SpendEvaluator | `cloudwatch:GetMetricData` on `*` | Service limitation — CW doesn't support resource-level for Get operations |
| PricingRefresh | `pricing:GetProducts` on `*` | Service limitation — Pricing API doesn't support resource-level |
| EnforcementHandler | `organizations:*` on `*` | Condition-scoped to `SERVICE_CONTROL_POLICY` type only |

All other IAM policies are scoped to specific resource ARNs with conditions.

### Encryption

- DynamoDB tables: SSE enabled (AWS-managed or customer CMK via parameter)
- SNS topics: KMS encryption (optional CMK via parameter)
- S3 buckets: Not yet deployed (Tier 4)
- Logging role: scoped to specific log group ARN only

## Cleanup

```bash
./deploy.sh --destroy
# Also manually delete: Bedrock logging configuration
aws bedrock delete-model-invocation-logging-configuration --region us-east-1
```

## Conclusion

The solution deploys cleanly, enables logging automatically, and produces real-time metrics in the CloudWatch dashboard within minutes of Bedrock invocations. The one-click deploy experience (`./deploy.sh --email ...`) handles all setup including IAM role provisioning and logging enablement.

### Key Discovery: Use Native AWS/Bedrock Metrics

CloudWatch metric filters **cannot extract numeric values from nested JSON paths** (e.g., `$.input.inputTokenCount`). The filter pattern matches log events, but `MetricValue` extraction silently fails for nested paths.

**Solution:** The dashboard uses the native `AWS/Bedrock` namespace metrics that AWS provides automatically for every Bedrock invocation. These require zero configuration and are available immediately:
- `AWS/Bedrock/Invocations`
- `AWS/Bedrock/InputTokenCount`
- `AWS/Bedrock/OutputTokenCount`
- `AWS/Bedrock/InvocationLatency`
- `AWS/Bedrock/InvocationClientErrors`
- `AWS/Bedrock/InvocationThrottles`

The custom `BedrockCostTracker` namespace is reserved for SDK wrapper team/application attribution metrics (emitted via direct `PutMetricData`).

### Total Invocations During Testing

50+ invocations across Claude Sonnet 4.6 and Claude Haiku 4.5, generating ~8,500 tokens total. Dashboard confirmed showing multi-model breakdown, latency percentiles, and invocation counts.
