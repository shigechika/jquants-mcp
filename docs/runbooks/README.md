# Incident runbooks

One runbook per alert scenario. Each is ≤ 1 screen: **symptom → quick check
→ root cause options → recovery → post-incident**.

| Runbook | Triggered by alert |
|---|---|
| [oom.md](oom.md) | `Cloud Run memory > 90%`, `Cloud Run OOM kill` |
| [5xx-spike.md](5xx-spike.md) | `Cloud Run 5xx rate > 1%` |
| [firestore-outage.md](firestore-outage.md) | `Firestore error rate > 5%` |
| [cache-db-missing.md](cache-db-missing.md) | `cache.db download failed` |
| [oauth-loop.md](oauth-loop.md) | Manual (user reports login failure) |
| [firestore-restore.md](firestore-restore.md) | Manual (data loss) |
| [secrets-rotation.md](secrets-rotation.md) | Manual (planned / leak response) |
| [plan-upgrade.md](plan-upgrade.md) | Manual (J-Quants plan upgrade / downgrade) |

Alert policies live in [`ops/alerts/`](../../ops/alerts/). Each policy's
`documentation.content` links back to the matching runbook here.
