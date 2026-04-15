# GCP billing budgets

## Active budgets (billing account `019C8B-42A471-9512F0`)

| Scope | Amount | Thresholds |
|---|---|---|
| Account-wide | ¥320 / month | 50 / 90 / 100 / 150% |
| `aikawa-dx` (project) | ¥500 / month | 50 / 80 / 100% |

The project-scoped budget sends alerts to the same `shige@aikawa.jp`
notification channel used by the Cloud Monitoring alerts in `ops/alerts/`.

## Create (reference)

```sh
gcloud billing budgets create \
  --billing-account=019C8B-42A471-9512F0 \
  --display-name="aikawa-dx (jquants-dat-mcp) monthly" \
  --budget-amount=500JPY \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.8 \
  --threshold-rule=percent=1.0 \
  --filter-projects=projects/aikawa-dx \
  --credit-types-treatment=include-all-credits \
  --notifications-rule-monitoring-notification-channels=projects/aikawa-dx/notificationChannels/<ID>
```

## Inspect

```sh
gcloud billing budgets list \
  --billing-account=019C8B-42A471-9512F0 \
  --format="value(displayName,amount.specifiedAmount.units,amount.specifiedAmount.currencyCode)"
```

## Tuning

¥500 is a generous starting ceiling for Cloud Run (1 vCPU / 4 GiB, scale-to-zero)
+ Firestore (< 1 MiB) + GCS (cache.db ~3.5 GiB, one-way replicated). Actual
spend is expected to be single-digit yen per month. If the 50% alert ever
fires, investigate before raising the ceiling.
