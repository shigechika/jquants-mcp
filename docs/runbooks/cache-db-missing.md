# cache.db missing / download failed / loaded stale

Covers two related alerts that both point here:

- **`cache.db download failed`** (`05-cache-db-download-fail.yaml`, ERROR) — the
  download itself never completed.
- **`cache.db loaded stale`** (`07-cache-stale.yaml`, WARNING) — the download
  succeeded, but the loaded snapshot's latest equities date is more than a
  week behind today, meaning the self-hosted publisher likely stopped
  pushing fresh snapshots.

## Symptom

**Download failed:**
- Alert `cache.db download failed` firing
- `cache_status` returns minimal payload > 5 minutes after cold start
- Tool responses have `"source": "api"` persistently (no Tier 1 cache hits)

**Loaded stale:**
- Alert `cache.db loaded stale` firing (log pattern `"cache.db is stale"`,
  emitted by `CacheStore._log_cache_freshness` on load/reload)
- Server is otherwise healthy and serving Tier 1 cache hits normally, but
  the latest cached date is stuck > 7 days behind today
- Tool calls for recent dates silently fall back to the live J-Quants API
  (slower, rate-limit prone) instead of erroring

Service stays *up* via live J-Quants API fallback in both cases — this is a
degraded mode, not an outage.

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

# Confirm which alert fired: "cache.db is stale" (loaded-stale) vs a
# download-failure error in the same window
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-mcp"
   textPayload:"cache.db is stale"' \
  --project=${PROJECT} --limit=5 --freshness=1h
```

## Root cause options

**Download failed:**
1. **GCS object missing** — the self-hosted daily refresh script failed to
   upload. Check the cron / scheduled-task status.
2. **IAM drift** — SA lost `roles/storage.objectViewer`. Re-grant.
3. **Transient GCS error** — restart and retry.
4. **Disk full on `/tmp`** — unlikely but possible if another process
   wrote large files. Cold restart clears tmpfs.

**Loaded stale:**
1. **Publisher stopped running** — the self-hosted host's `daily_fetch.py` /
   `gcs_export_cache.py` cron/launchd job silently stopped (host offline,
   J-Quants API key expired, disk full, etc.). This is the most common
   cause — check the publisher host directly, not Cloud Run.
2. **Publisher runs but uploads fail** — `daily_fetch.py` succeeds locally
   but `gcs_export_cache.py` errors before the upload (e.g. `verify_cache_completeness.py`
   catching an incomplete fetch and exiting non-zero, which the bundled
   `scripts/daily-fetch.crontab` treats as a hard stop before the export step).
3. **No reload has run since the publisher recovered** — this alert only
   fires on a load/reload event (startup or a Pub/Sub-triggered reload); a
   publisher that resumes pushing fresh snapshots without a Cloud Run
   restart or `jquants-mcp-cache-updated` publish won't clear the condition
   until the next reload happens.

## Recovery

- **Stale / missing object**: re-run the daily refresh on the publisher
  host, for example:
  ```sh
  uv run python scripts/daily_fetch.py
  uv run python scripts/gcs_export_cache.py
  ```
- **IAM**: `gcloud projects add-iam-policy-binding ${PROJECT} --member=serviceAccount:jquants-mcp@${PROJECT}.iam.gserviceaccount.com --role=roles/storage.objectViewer`
- **Force a retry (download failed)**: send SIGHUP or deploy a new revision
  ```sh
  gcloud run services update jquants-mcp --region=us-west1 \
    --project=${PROJECT} --update-labels=kick=$(date +%s)
  ```
- **Force a reload (loaded stale, after the publisher has a fresh snapshot)**:
  publish the Pub/Sub topic so Cloud Run picks it up without waiting for a
  cold start
  ```sh
  gcloud pubsub topics publish jquants-mcp-cache-updated --project=${PROJECT}
  ```

## Post-incident

- Confirm the next scheduled daily refresh succeeds
- If the schedule was down, check your cron / launchd / systemd service
- For a stale-load incident, confirm the reload actually cleared the
  condition: no further `"cache.db is stale"` log lines after the next
  load/reload, and `health_check` / `cache_status` show a recent latest date
