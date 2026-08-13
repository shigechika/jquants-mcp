# Cloud Run alert policies

Declarative alert policies for the Cloud Run `jquants` deployment.
Each `*.yaml` is a single policy; `__CHANNEL__` is a placeholder substituted
at deploy time.

## Policies

| File | Severity | Condition |
|---|---|---|
| `01-memory-high.yaml` | WARNING | memory p95 > 90% for 5 min |
| `02-5xx-rate.yaml` | WARNING | non-502 5xx / total > 1% |
| `03-oom-kill.yaml` | CRITICAL | OOM kill log match |
| `04-firestore-errors.yaml` | WARNING | Firestore non-2xx > 5% for 10 min |
| `05-cache-db-download-fail.yaml` | WARNING | cache.db download failure log match |
| `06-no-instances.yaml` | WARNING (disabled) | instance_count == 0 for 1 h |
| `07-cache-stale.yaml` | WARNING | stale cache.db loaded (latest equities date > 1 week behind) log match |

`06` is disabled and should stay that way. `min-instances=0` is deliberate —
scaling to zero is what keeps the cache fresh, since every cold start
re-downloads a current `cache.db` (#584). Zero instances for an hour is the
normal idle state, so enabling this policy pages on healthy behaviour.
Answering "is the service reachable?" needs a probe that tells idle apart from
broken, not an instance-count threshold.

`02` excludes 502 deliberately. With `min-instances=0` a cold start spends
~17s downloading `cache.db` before the gateway binds, and the sidecar answers
502 for that entire window — paging on it means paging on scale-to-zero
working as designed (it fired that way on 2026-08-13, from three CD smoke-test
requests). Over the 12 hours around that incident, excluding 502 leaves the
query with no data points at all: every 5xx observed was a cold start.

What `02` gives up is the crash-looping app container, and **nothing currently
covers that case.** A CRITICAL "sustained 502" policy was written for this PR
and then withdrawn, because measuring it against real traffic showed it would
page on healthy behaviour. The reason is traffic volume, not query tuning: six
hours of production carried eight requests in total, with 502s at 07:06 (×3)
and 07:30 (×1) — two ordinary cold starts, 24 minutes apart. A 15-minute
aligner stepping every 15 minutes puts both bursts into two *consecutive*
non-zero windows whenever the window boundary lands between 07:15 and 07:21,
so roughly two phases in five satisfy `duration: 900s` and page. Two
back-to-back queries of the same data disagreed for exactly this reason.

At this request volume, "502s that never stop" is not distinguishable from
"two cold starts in half an hour" by rate arithmetic. Detecting a crash-loop
needs a signal that does not depend on someone happening to send a request —
a log match on the app container failing to start, in the style of `03` and
`05`, or an external probe. Tracked as follow-up; do not re-introduce a
rate-based version without measuring it against live traffic first.

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
