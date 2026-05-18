# Solution Brief: Amazon Bedrock Cost Tracker

## Executive Summary

A progressive, multi-tier cost tracking and governance solution for Amazon Bedrock. Provides per-team/application cost attribution, real-time dashboards, budget alerts, and automated enforcement (warn → throttle → shutoff) — all deployable in your own account with a single command.

## Problem Statement

Every organization adopting Amazon Bedrock faces the same challenge:

- **No native per-team cost attribution** — Bedrock costs appear as a single line item in Cost Explorer
- **No real-time spend visibility** — CUR data has 24-hour lag; teams don't know they're overspending until the bill arrives
- **No automated enforcement** — Budget alerts notify but don't prevent overspend
- **Shadow AI usage** — Teams invoke models directly without governance, making cost allocation impossible
- **Multi-account complexity** — Organizations need centralized visibility across dozens of accounts

Existing options:
- **Cost Explorer alone** — No per-team breakdown, no real-time, no enforcement
- **Third-party FinOps tools** — Expensive, don't understand Bedrock token economics, no enforcement
- **Custom solutions** — Every team builds their own, inconsistent, unmaintained

## Solution

A 5-tier progressive architecture — deploy only what you need:

| Tier | Capability | Deployment |
|------|-----------|------------|
| 1 | Cost Explorer + Budgets + Anomaly Detection | Config guide (free) |
| 2 | Inference Profiles + Cost Allocation Tags | Config guide (free) |
| 3 | CloudWatch Metrics + Dashboards + Alarms | `sam deploy` (~$3-80/mo) |
| 4 | CUR + Athena Analytics + QuickSight | `sam deploy` (~$5-120/mo) |
| 5 | Cost Sentry (automated enforcement) | `sam deploy` (~$8-200/mo) |

**Key differentiator:** The SDK wrapper instruments Bedrock calls with <50ms overhead, capturing per-request token counts and cost with team/application attribution. No code changes beyond wrapping your client.

## Differentiation

| Capability | This Solution | Cost Explorer Only | Third-Party FinOps |
|-----------|---------------|-------------------|-------------------|
| Per-team attribution | ✅ (SDK wrapper + tags) | ❌ | Partial |
| Real-time visibility | ✅ (CloudWatch, <1 min) | ❌ (24h lag) | Varies |
| Automated enforcement | ✅ (warn/throttle/shutoff) | ❌ | ❌ |
| Token-level tracking | ✅ (input/output/total) | ❌ | ❌ |
| Multi-account | ✅ (Organizations + cross-account roles) | Partial | ✅ |
| Cost | $3-400/month | $0 | $500-5K/month |
| Deployment | Your account (SAM) | N/A | SaaS |
| Customizable | ✅ (open source) | N/A | ❌ |

## Target Customer

- Any organization using Amazon Bedrock at scale
- FinOps teams managing AI/ML spend
- Platform teams providing Bedrock as a shared service
- Enterprises with multi-account AWS Organizations
- Startups wanting cost guardrails before spend gets out of control

## Architecture Highlights

- **SDK Wrapper** — Python package, pip-installable, <50ms overhead, supports invoke_model, converse, and streaming
- **Cost Sentry** — Step Functions state machine that evaluates spend every 5 minutes and applies IAM deny policies or SCPs when budgets are breached
- **Reconciliation** — Daily CUR vs. CloudWatch reconciliation catches any tracking gaps
- **Pricing Refresh** — Weekly automated refresh from AWS Pricing API ensures cost calculations stay accurate
- **Cross-Account** — IAM role templates + SCP for Organizations-wide enforcement

## Security Posture

- IAM authentication on all API endpoints
- Optional KMS CMK encryption on all DynamoDB tables and SNS topics
- SCP enforcement prevents bypass of Inference Profile requirement
- Audit log with TTL for all enforcement actions
- MFA option for restore-access endpoint (re-enabling after shutoff)
- No data leaves your account

## Cost

The tracker itself costs less than what it saves in the first hour:

| Scale | Monthly Cost (All Tiers) |
|-------|-------------------------|
| 1K Bedrock requests/day | ~$16/month |
| 100K requests/day | ~$80/month |
| 1M requests/day | ~$400/month |

Typical ROI: prevents $5K-50K/month in uncontrolled Bedrock spend.

## Implementation Timeline

- **Hour 1:** Deploy Tier 3, install SDK wrapper, see metrics flowing
- **Day 1:** Configure Tier 1 & 2 (budgets, Inference Profiles, tags)
- **Week 1:** Deploy Tier 4 for historical analytics, set up team dashboards
- **Week 2:** Deploy Tier 5 Cost Sentry with conservative thresholds
- **Ongoing:** Tune thresholds based on actual usage patterns

## Related AWS Solutions

- No existing AWS Solution for Bedrock-specific cost governance
- Complements AWS Budgets (adds real-time + enforcement)
- Complements Cost Explorer (adds per-team attribution)
- Works alongside AWS Organizations SCPs (adds Bedrock-specific enforcement)
