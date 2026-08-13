# 5xx spike

## Symptom

- Alert `Cloud Run 5xx rate > 1%` firing
- Users report tool call failures

## Quick check

```sh
# Top error in the last hour
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants"
   severity>=ERROR' \
  --project=${PROJECT} --limit=20 --format=json --freshness=1h \
  | jq -r '.[].textPayload // .[].jsonPayload.message' | sort | uniq -c | sort -rn

# Recent deploys (look for the one right before the spike)
gcloud run revisions list --service=jquants \
  --region=us-west1 --project=${PROJECT} --limit=5

# Firestore and J-Quants API status
curl -sI https://api.jquants.com/v2/token/auth_refresh | head -1
```

## Root cause options

1. **Recent deploy bug** — spike starts at deploy time.
2. **Firestore outage / quota** — see [firestore-outage.md](firestore-outage.md).
3. **J-Quants API outage** — external, nothing to do but wait. Confirm on <https://status.jpx-jquants.com> if it exists.
4. **cache.db corrupted or missing** — see [cache-db-missing.md](cache-db-missing.md).
5. **Gateway auth failure** — since 1.0.0 this package has no OAuth code, so a
   5xx from the auth layer originates in the `oauth2-proxy` sidecar or the
   `mcp-stdio serve` gateway, not here. See [oauth-loop.md](oauth-loop.md),
   which opens by identifying which layer a given failure came from.

## Recovery

**Rollback to previous revision** (if correlated with a deploy):

```sh
gcloud run services update-traffic jquants \
  --region=us-west1 --project=${PROJECT} \
  --to-revisions=<PREVIOUS_REVISION>=100
```

Follow up with a fix-forward PR; traffic rollback via CLI is a temporary
measure that will be overwritten by the next CD deploy.

**Disable a specific tool** as a last resort: add a guard in the tool's
`register()` function returning `{"error": "temporarily disabled"}` and deploy.

## Post-incident

- File an issue describing the trigger, blast radius, and root cause
- If the fix was non-trivial, add a regression test
