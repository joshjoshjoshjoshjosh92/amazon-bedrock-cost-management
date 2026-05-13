# Tier 1: Cost Explorer, Budgets, and Anomaly Detection

This guide walks you through setting up native AWS cost visibility for Amazon Bedrock. No infrastructure deployment is required — everything is configured through the AWS Console or CLI.

**Time to complete:** ~15 minutes

## Prerequisites

- AWS account with Bedrock enabled
- IAM permissions: `ce:*`, `budgets:*`, `ce:CreateAnomalyMonitor`, `ce:CreateAnomalySubscription`
- Cost Explorer activated (takes up to 24 hours on first enable)

---

## Step 1: Explore Bedrock Costs in Cost Explorer

Cost Explorer provides a visual breakdown of your Bedrock spend by usage type, model, and time period.

### Console Steps

1. Open the [AWS Cost Explorer console](https://console.aws.amazon.com/cost-management/home#/cost-explorer)
2. Set the date range to the last 30 days (or your preferred window)
3. Under **Filters**, click **Service** and select **Amazon Bedrock**
4. Under **Group by**, select **Usage Type** to see a breakdown by model and operation

   Common usage types:
   - `USE1-InvokeModel:anthropic.claude-3-sonnet` — On-demand inference
   - `USE1-ProvisionedThroughput` — Provisioned capacity
   - `USE1-ModelCustomization` — Fine-tuning jobs

5. Switch **Group by** to **API Operation** to see `InvokeModel` vs `Converse` vs batch jobs
6. Use **Tag** grouping if you have cost allocation tags activated (see Tier 2 guide)

### CLI Alternative

```bash
# Get Bedrock costs for the last 30 days grouped by usage type
aws ce get-cost-and-usage \
  --time-period Start=$(date -d '30 days ago' +%Y-%m-%d),End=$(date +%Y-%m-%d) \
  --granularity DAILY \
  --metrics "UnblendedCost" \
  --filter '{"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE
```

---

## Step 2: Create a Bedrock Budget Alert

AWS Budgets sends notifications when your Bedrock spend approaches or exceeds a threshold.

### Console Steps

1. Open the [AWS Budgets console](https://console.aws.amazon.com/billing/home#/budgets)
2. Click **Create budget**
3. Select **Customized (advanced)** → **Cost budget** → **Next**
4. Configure the budget:
   - **Budget name:** `bedrock-monthly-budget`
   - **Period:** Monthly
   - **Budget amount:** Fixed — enter your monthly Bedrock budget (e.g., `$500`)
   - **Start month:** Current month
5. Under **Budget scope**, click **Add filter**:
   - Filter type: **Service**
   - Value: **Amazon Bedrock**
6. Click **Next** to configure alerts
7. Add **Alert 1 — Early warning (80%)**:
   - Threshold: `80`% of budgeted amount
   - Trigger: **Actual**
   - Notification: Enter your email or SNS topic ARN
8. Add **Alert 2 — Budget exceeded (100%)**:
   - Threshold: `100`% of budgeted amount
   - Trigger: **Actual**
   - Notification: Enter your email or SNS topic ARN
9. (Optional) Add **Alert 3 — Forecasted breach**:
   - Threshold: `100`% of budgeted amount
   - Trigger: **Forecasted**
   - Notification: Same recipients
10. Click **Next** → **Create budget**

### CLI Alternative

```bash
aws budgets create-budget \
  --account-id $(aws sts get-caller-identity --query Account --output text) \
  --budget '{
    "BudgetName": "bedrock-monthly-budget",
    "BudgetLimit": {"Amount": "500", "Unit": "USD"},
    "BudgetType": "COST",
    "TimeUnit": "MONTHLY",
    "CostFilters": {"Service": ["Amazon Bedrock"]}
  }' \
  --notifications-with-subscribers '[
    {
      "Notification": {
        "NotificationType": "ACTUAL",
        "ComparisonOperator": "GREATER_THAN",
        "Threshold": 80,
        "ThresholdType": "PERCENTAGE"
      },
      "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "your-email@example.com"}]
    },
    {
      "Notification": {
        "NotificationType": "ACTUAL",
        "ComparisonOperator": "GREATER_THAN",
        "Threshold": 100,
        "ThresholdType": "PERCENTAGE"
      },
      "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "your-email@example.com"}]
    }
  ]'
```

---

## Step 3: Enable Cost Anomaly Detection for Bedrock

Cost Anomaly Detection uses machine learning to identify unusual spend patterns. It requires ~2 weeks of historical data to establish a baseline.

### Console Steps

1. Open the [Cost Anomaly Detection console](https://console.aws.amazon.com/cost-management/home#/anomaly-detection)
2. Click **Create monitor**
3. Configure the monitor:
   - **Monitor type:** AWS Service
   - **Monitor name:** `bedrock-anomaly-monitor`
4. Click **Next**
5. Create an alert subscription:
   - **Subscription name:** `bedrock-anomaly-alerts`
   - **Threshold:** Alert when impact exceeds **$10** (adjust based on your baseline spend)
   - **Frequency:** Individual alerts (immediate notification)
   - **Recipients:** Enter email addresses or SNS topic ARN
6. Click **Create monitor**

### CLI Alternative

```bash
# Create the anomaly monitor
aws ce create-anomaly-monitor \
  --anomaly-monitor '{
    "MonitorName": "bedrock-anomaly-monitor",
    "MonitorType": "DIMENSIONAL",
    "MonitorDimension": "SERVICE"
  }'

# Note the MonitorArn from the output, then create a subscription
aws ce create-anomaly-subscription \
  --anomaly-subscription '{
    "SubscriptionName": "bedrock-anomaly-alerts",
    "MonitorArnList": ["arn:aws:ce::123456789012:anomalymonitor/MONITOR_ID"],
    "Subscribers": [{"Type": "EMAIL", "Address": "your-email@example.com"}],
    "Threshold": 10.0,
    "Frequency": "IMMEDIATE"
  }'
```

---

## What You Get

After completing Tier 1, you have:

| Capability | Latency | Cost |
|-----------|---------|------|
| Visual spend breakdown by model and usage type | 24-48 hours | $0 |
| Budget alerts at 80% and 100% thresholds | ~4 hours | $0 |
| ML-based anomaly detection | ~24 hours | $0 |

## Limitations

- Cost Explorer data is delayed 24-48 hours — not suitable for real-time enforcement
- Budget alerts check approximately every 4 hours — a burst can exceed your budget before the alert fires
- Anomaly Detection needs ~2 weeks of data to establish a baseline
- No per-request or per-team granularity without cost allocation tags (see Tier 2)

## Next Steps

- [Tier 2: Application Inference Profiles and Cost Allocation Tags](tier2-inference-profiles.md) — Add per-team and per-application cost attribution
- Tier 3: CloudWatch Monitoring — Deploy real-time token-level metrics (`sam deploy`)
