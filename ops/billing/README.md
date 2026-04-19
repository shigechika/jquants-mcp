# GCP billing budgets

Reference for setting up cost alerts on your own Cloud Run deployment of
jquants-mcp. Replace placeholders (`<BILLING_ACCOUNT_ID>`, `<PROJECT_ID>`,
`<CHANNEL_ID>`) with your own values.

## Suggested budgets

| Scope | Amount | Thresholds |
|---|---|---|
| Account-wide | small ceiling | 50 / 90 / 100 / 150% |
| Project-scoped | small ceiling | 50 / 80 / 100% |

The project-scoped budget should forward alerts to the same notification
channel used by the Cloud Monitoring alerts in `ops/alerts/` (email works
fine for a one-person operation).

## Create (reference)

```sh
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="<PROJECT_ID> (jquants-mcp) monthly" \
  --budget-amount=500JPY \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.8 \
  --threshold-rule=percent=1.0 \
  --filter-projects=projects/<PROJECT_ID> \
  --credit-types-treatment=include-all-credits \
  --notifications-rule-monitoring-notification-channels=projects/<PROJECT_ID>/notificationChannels/<CHANNEL_ID>
```

## Inspect

```sh
gcloud billing budgets list \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --format="value(displayName,amount.specifiedAmount.units,amount.specifiedAmount.currencyCode)"
```

## Tuning

For a solo Cloud Run deployment (1 vCPU / 4 GiB, scale-to-zero) plus
Firestore (< 1 MiB) and GCS (cache.db ~3.5 GiB, one-way replicated), actual
spend typically lands in the single-digit yen per month. A ¥500 ceiling gives
plenty of margin. If the 50% alert ever fires, investigate before raising it.
