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
gcloud storage ls -l gs://aikawa-dx-jquants-dat-mcp/cache.db

# entrypoint.sh download logs
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-dat-mcp"
   (textPayload:"cache.db" OR textPayload:"gcs_sync")' \
  --project=aikawa-dx --limit=30 --freshness=1h

# SA has objectViewer on the bucket?
gcloud storage buckets get-iam-policy gs://aikawa-dx-jquants-dat-mcp \
  --format=json | jq '.bindings[] | select(.members[] | contains("jquants-dat-mcp@"))'
```

## Root cause options

1. **GCS object missing or stale** — self-hosted `jpx-short-report/daily.sh`
   failed to upload. Check the self-hosted cron status.
2. **IAM drift** — SA lost `roles/storage.objectViewer`. Re-grant.
3. **Transient GCS error** — restart and retry.
4. **Disk full on `/tmp`** — unlikely but possible if another process
   wrote large files. Cold restart clears tmpfs.

## Recovery

- **Stale / missing object**: re-run the upload from `m1.local`:
  ```sh
  cd ~/src/kb/jpx-short-report && ./daily.sh   # steps 4-8 export cache.db
  ```
- **IAM**: `gcloud projects add-iam-policy-binding aikawa-dx --member=serviceAccount:jquants-dat-mcp@aikawa-dx.iam.gserviceaccount.com --role=roles/storage.objectViewer`
- **Force a retry**: send SIGHUP or deploy a new revision
  ```sh
  gcloud run services update jquants-dat-mcp --region=us-west1 \
    --project=aikawa-dx --update-labels=kick=$(date +%s)
  ```

## Post-incident

- Confirm `daily.sh` succeeded subsequently
- If cron was down, check the self-hosted launchd service on m1.local
