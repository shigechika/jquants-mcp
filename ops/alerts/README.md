# Cloud Run alert policies

Declarative alert policies for the Cloud Run `jquants` deployment.
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
| `08-upstream-down.yaml` | CRITICAL | sidecar 502s continuously for 15 min (app container not coming up) |

`06` is disabled and should stay that way. `min-instances=0` is deliberate —
scaling to zero is what keeps the cache fresh, since every cold start
re-downloads a current `cache.db` (#584). Zero instances for an hour is the
normal idle state, so enabling this policy pages on healthy behaviour.
Answering "is the service reachable?" needs a probe that tells idle apart from
broken, not an instance-count threshold.

`02` excludes 502 deliberately, and `08` exists because of that exclusion.
With `min-instances=0` a cold start spends ~17s downloading `cache.db` before
the gateway binds, and the sidecar answers 502 for that entire window — paging
on it means paging on scale-to-zero working as designed (it fired that way on
2026-08-13, from three CD smoke-test requests). What `02` gives up is the
crash-looping app container, so `08` catches that instead: not a ratio, but
502s that simply never stop.

`07` fires when a session's child process first opens a stale `cache.db`, not
at container startup — `verify_cache.py` never constructs a `CacheStore`, so a
cold start that pulls a stale snapshot and is never queried logs nothing. In
practice the first real tool call surfaces it, and a snapshot nobody queries is
one nobody is served from. It cannot detect a publisher that stops *after* a
good load. See the policy's `documentation` block for the external-check
follow-up.

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
