# OOM / memory pressure

## Symptom

- Alert `Cloud Run memory > 90%` or `Cloud Run OOM kill` firing
- Requests return 5xx (container restart) or time out

## Quick check

```sh
# Recent OOM kills
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-dat-mcp"
   (textPayload:"Memory limit" OR textPayload:"OOMKilled")' \
  --project=aikawa-dx --limit=10 --format=json --freshness=1h

# cache.db size on the live instance
mcp call health_check   # via CLI or Claude
mcp call cache_status

# Recent deploys
gcloud run revisions list --service=jquants-dat-mcp \
  --region=us-west1 --project=aikawa-dx --limit=5
```

## Root cause options

1. **cache.db bloat** — GCS snapshot grew past load-test headroom
   (~3.9 GiB peak at 6 GiB ceiling). Check `gcloud storage ls -l
   gs://aikawa-dx-jquants-dat-mcp/cache.db`.
2. **Response-cache runaway** — Tier2 entries piling up. `cache_clear(table="response_cache")`.
3. **Recent deploy regression** — new code allocating more. Rollback.
4. **True memory leak** — container memory rising over time, not
   correlated with request volume.

## Recovery

**Emergency ceiling bump** (buy time, follow up with measurement):

```sh
# Edit .github/workflows/cd.yml, bump --memory, merge to main.
# Do NOT use `gcloud run services update` directly — CD will overwrite it.
```

**Normal path**: reproduce with `scripts/load_test.py`, determine the new
baseline, raise ceiling via PR. See `docs/cloud-run-memory-sizing.md` for
the methodology.

## Post-incident

- Re-run `scripts/load_test.py` against the new ceiling
- Update `docs/cloud-run-memory-sizing.md` if the baseline shifted
- Consider adding an alert at 80% if the ceiling was raised
