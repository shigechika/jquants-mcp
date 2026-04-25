# Firestore outage / quota

## Symptom

- Alert `Firestore error rate > 5%` firing
- Users cannot authenticate or see `UserNotConfiguredError` after previously being configured
- Server logs show `google.api_core.exceptions` traces

## Quick check

```sh
# Recent Firestore errors
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-mcp"
   (textPayload:"firestore" OR jsonPayload.message:"firestore")
   severity>=ERROR' \
  --project=aikawa-dx --limit=20 --freshness=1h

# Firestore health via SA
gcloud firestore databases list --project=aikawa-dx
gcloud firestore documents list --database='(default)' \
  --collection=users --limit=1 --project=aikawa-dx

# Service account IAM
gcloud projects get-iam-policy aikawa-dx \
  --flatten=bindings \
  --filter='bindings.members=serviceAccount:jquants-mcp@aikawa-dx.iam.gserviceaccount.com' \
  --format='value(bindings.role)'
# Expected: datastore.user (and objectViewer, secretmanager.secretAccessor)
```

## Root cause options

1. **IAM drift** — SA lost `roles/datastore.user`. Re-grant.
2. **Quota exceeded** — check the Firestore usage dashboard. Free tier is generous (50K reads / 20K writes per day) for this workload.
3. **Regional outage** — [GCP status dashboard](https://status.cloud.google.com/).
4. **Client-library bug** — rare; check if recent dependency bump changed the google-cloud-firestore version.

## Recovery

- **IAM**: `gcloud projects add-iam-policy-binding aikawa-dx --member=serviceAccount:jquants-mcp@aikawa-dx.iam.gserviceaccount.com --role=roles/datastore.user`
- **Quota**: request an increase in the Quotas page; for this workload the quota limit should not be hit without abuse — investigate rate-limit logs first
- **Regional outage**: wait; no action. Server degrades gracefully — tool calls that don't need user lookup still work.

## Post-incident

- If IAM drift: check whether any automation or CD step is resetting it
- If quota: consider whether per-user rate limiting (see `rate_limit.py`) needs tightening
