# cache.db missing / download failed

## Symptom

- Alert `cache.db download failed` firing
- `cache_status` returns minimal payload > 5 minutes after cold start
- Tool responses have `"source": "api"` persistently (no Tier 1 cache hits)

Service stays *up* via live J-Quants API fallback — this is a degraded
mode, not an outage.

## Quick check

```sh
# GCS object exists and is recent?
gcloud storage ls -l gs://${BUCKET}/cache.db

# entrypoint.sh download logs
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-mcp"
   (textPayload:"cache.db" OR textPayload:"gcs_sync")' \
  --project=${PROJECT} --limit=30 --freshness=1h

# SA has objectViewer on the bucket?
gcloud storage buckets get-iam-policy gs://${BUCKET} \
  --format=json | jq '.bindings[] | select(.members[] | contains("jquants-mcp@"))'
```

## Root cause options

1. **GCS object missing or stale** — the self-hosted daily refresh
   script failed to upload. Check the cron / scheduled-task status.
2. **IAM drift** — SA lost `roles/storage.objectViewer`. Re-grant.
3. **Transient GCS error** — restart and retry.
4. **Disk full on `/tmp`** — unlikely but possible if another process
   wrote large files. Cold restart clears tmpfs.

## Recovery

- **Stale / missing object**: re-run the daily refresh on the publisher
  host, for example:
  ```sh
  uv run python scripts/daily_fetch.py
  uv run python scripts/gcs_export_cache.py
  ```
- **IAM**: `gcloud projects add-iam-policy-binding ${PROJECT} --member=serviceAccount:jquants-mcp@${PROJECT}.iam.gserviceaccount.com --role=roles/storage.objectViewer`
- **Force a retry**: send SIGHUP or deploy a new revision
  ```sh
  gcloud run services update jquants-mcp --region=us-west1 \
    --project=${PROJECT} --update-labels=kick=$(date +%s)
  ```

## Post-incident

- Confirm the next scheduled daily refresh succeeds
- If the schedule was down, check your cron / launchd / systemd service
