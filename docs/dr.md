# Disaster recovery posture

Honest current-state description. An undocumented DR posture is worse than
a modest one — the goal here is to make the risks visible, not to advertise
nine-nines reliability.

## TL;DR

- **Region**: `us-west1` only. All three main components (Cloud Run,
  Firestore, GCS bucket `${BUCKET}`) live there.
- **RTO** (time to recover from a regional outage): **hours** — requires
  manual redeploy to a standby region plus DNS / OAuth redirect updates.
- **RPO** (data loss on catastrophic failure): **0** for `users` and the
  gateway's OAuth token store (Firestore writes are synchronous and backed up
  daily); **up to 1 day** for `cache.db` (depending on self-hosted publisher
  cadence).
- **No automated failover.** This is a conscious choice for the current
  user count and cost envelope.

## Components and their failure modes

### Cloud Run (`jquants`, `us-west1`)

| Risk | Impact | Mitigation today |
|---|---|---|
| `us-west1` regional outage | Service offline | None — redeploy to standby region (see below) |
| Bad deploy | Service 5xx | Manual rollback via `gcloud run services update-traffic`; see [5xx runbook](runbooks/5xx-spike.md) |
| CD misconfiguration | Service offline | CD workflow in `.github/workflows/cd.yml` is the source of truth; revert the commit |

### Firestore (`(default)`, `us-west1`)

Verified: `gcloud firestore databases describe --database='(default)'`
shows `locationId: us-west1` (single-region, **not** `nam5` multi-region).

| Risk | Impact | Mitigation today |
|---|---|---|
| `us-west1` regional outage | Login / API-key lookup fails | Managed backups in a different region for restore (see [firestore-restore](runbooks/firestore-restore.md)) |
| Accidental collection delete | Data loss | Daily + weekly managed backups (#84) |
| Quota exceeded | Auth errors | See [firestore-outage runbook](runbooks/firestore-outage.md) |

### cache.db on GCS (`gs://${BUCKET}`)

| Risk | Impact | Mitigation today |
|---|---|---|
| Publisher (self-hosted) dies | No fresh data until publisher restored. Cloud Run keeps serving stale snapshot, falls back to live API | Publisher rebuild steps below |
| GCS object corrupted | Fresh cold starts lose Tier 1 cache, API fallback kicks in | Re-upload from publisher; [cache-db-missing runbook](runbooks/cache-db-missing.md) |
| `us-west1` outage | Cloud Run cannot read bucket | Bucket is currently regional; could be promoted to multi-region if needed |

## RTO / RPO table

| Scenario | RTO | RPO |
|---|---|---|
| Bad deploy | 5 min (CLI traffic rollback) | 0 |
| Firestore collection accidentally dropped | 30 min (restore to scratch DB, repoint Cloud Run) | Up to 24 h (last daily backup) |
| Publisher dies | Hours (rebuild on spare machine) | Up to 1 day (next successful publish) |
| Full `us-west1` regional outage | Hours (manual redeploy to standby region, OAuth redirect updates, DNS if needed) | 0 for Firestore, up to 1 day for cache.db |

## Standby region procedure (not yet deployed, documented only)

If `us-west1` is down for extended period and we decide to fail over:

1. **Pick a region** with GCP Cloud Run + Firestore support, e.g.
   `us-east1` or `asia-northeast1` (Tokyo) for latency.
2. **Create the service in the standby region.** It is a **two-container**
   service — an `oauth2-proxy` sidecar as ingress plus the app container
   started with `--command /app/scripts/entrypoint-stdio.sh`. A single
   `gcloud run deploy --source=.` will not reproduce it: that yields one
   container with no auth in front, reachable by anyone.

   [`docs/deploy/gcp.md`](deploy/gcp.md) is the canonical creation procedure —
   follow it against `<STANDBY>`. `cd.yml` is not a substitute: it only
   updates an existing service's app image (`services update --container app
   --image`) and deliberately never touches the sidecar, scaling, env or
   secrets.
3. **Firestore**: restore latest managed backup into a new database in the
   standby region, point Cloud Run at it via env var.
4. **Public URL**: point everything that hard-codes the service URL at the new
   one — the app container's `PUBLIC_URL` env var (`mcp-stdio serve` advertises
   its OAuth metadata from it), the `OAUTH_BASE_URL` repo Variable (CD's
   post-deploy smoke test), and the authorized redirect URI on the sign-in
   layer's Google OAuth client. The sign-in layer is the `oauth2-proxy` sidecar,
   configured on the service; no OAuth client credentials live in this package.
5. **cache.db**: if the publisher is also down, Cloud Run serves via live
   J-Quants API fallback; otherwise repoint the publisher at the new
   region's bucket.

This procedure has **not been drilled**. Expected RTO is "hours", not
"minutes" — OAuth redirect propagation and DNS (if custom domain is
added later) are the slow steps.

## Minimal improvements (follow-up issues, not in this PR)

Decide per item whether it is worth the work:

- [ ] Promote cache.db bucket to multi-region (small cost bump, survives
      single-region outage)
- [ ] Run a quarterly DR drill: deploy to standby, restore Firestore
      backup, run smoke test, **measure actual RTO**. Document in
      `docs/dr-drill.md`. Highest leverage — turns theoretical RTO into
      measured RTO.
- [ ] Document the publisher rebuild steps more thoroughly. Currently
      lives in tribal knowledge on the self-hosted side.

## Explicitly out of scope

- **Active/active multi-region traffic routing** — cost and complexity
  are disproportionate to current user count.
- **Cross-region Firestore replication** — requires a separate write
  path; Firestore managed backups cover the data-loss case adequately.
- **Multi-account separation (prod / disaster)** — premature for
  solo-dev scale.
