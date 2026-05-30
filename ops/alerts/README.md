# Cloud Run alert policies

Declarative alert policies for the Cloud Run `jquants-mcp` deployment.
Each `*.yaml` is a single policy; `__CHANNEL__` is a placeholder substituted
at deploy time.

## Policies

| File | Severity | Condition |
|---|---|---|
| `01-memory-high.yaml` | WARNING | memory p95 > 90% for 5 min |
| `02-5xx-rate.yaml` | WARNING | 5xx / total > 1% for 10 min |
| `03-oom-kill.yaml` | CRITICAL | OOM kill log match |
| `04-firestore-errors.yaml` | WARNING | Firestore non-2xx > 5% for 10 min |
| `05-cache-db-download-fail.yaml` | WARNING | cache.db download failure log match |
| `06-no-instances.yaml` | WARNING (disabled) | instance_count == 0 for 1 h |
| `07-cache-stale.yaml` | WARNING | stale cache.db loaded (latest equities date > 1 week behind) log match |

`06` is disabled by default — cold-scale-to-zero is normal. Enable only if
you want to be paged when the service is idle longer than expected.

`07` fires only when a stale snapshot is loaded (startup / reload); it cannot
detect a publisher that stops *after* a good load. See the policy's
`documentation` block for the external-check follow-up.

## Deploy

```sh
# find or create the notification channel
gcloud beta monitoring channels list --project ${PROJECT} --format='table(displayName,type,name)'

CHANNEL="projects/${PROJECT}/notificationChannels/<ID>" ./ops/alerts/deploy.sh
```

The script is idempotent: existing policies matched by `displayName` are
updated, missing policies are created.

## Thresholds

Derived from the #72/#73 load test baselines (see
`docs/cloud-run-memory-sizing.md`). Tune after the first week of real data.
