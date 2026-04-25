# Runbook: Cloud Run service + SA + GCS bucket full rename

> One-shot runbook for migrating the `jquants-dat-mcp` deployment to
> the `jquants-mcp` naming throughout, completing what `#105` deferred
> as Option C. Pairs with `#106` (custom domain mapping).
>
> Status legend: `[ ]` not started, `[~]` in progress, `[x]` done.

## Scope

| Component | Old | New | Mutability |
|---|---|---|---|
| Cloud Run service | `jquants-dat-mcp` | `jquants-mcp` | Immutable — recreate |
| Service Account | `jquants-dat-mcp@aikawa-dx.iam.gserviceaccount.com` | `jquants-mcp@aikawa-dx.iam.gserviceaccount.com` | Immutable — recreate |
| GCS bucket | `aikawa-dx-jquants-dat-mcp` | `aikawa-dx-jquants-mcp` | Immutable — recreate |
| WIF SA principal | bound to old SA | rebind to new SA | Mutable |
| GH Actions secret `WIF_SERVICE_ACCOUNT` | old SA | new SA | Mutable |
| Custom domain | (none) | `jquants-mcp.aikawa.jp` | Additive |
| OAuth redirect URI | run.app URL | `https://jquants-mcp.aikawa.jp/oauth/callback` | Additive then prune |

## Variables

```bash
PROJECT_ID="aikawa-dx"
PROJECT_NUMBER="29004083822"
REGION="us-west1"

OLD_SERVICE="jquants-dat-mcp"
NEW_SERVICE="jquants-mcp"

OLD_SA="${OLD_SERVICE}@${PROJECT_ID}.iam.gserviceaccount.com"
NEW_SA="${NEW_SERVICE}@${PROJECT_ID}.iam.gserviceaccount.com"

OLD_BUCKET="aikawa-dx-${OLD_SERVICE}"
NEW_BUCKET="aikawa-dx-${NEW_SERVICE}"

CUSTOM_DOMAIN="jquants-mcp.aikawa.jp"
OLD_RUN_URL="https://jquants-dat-mcp-${PROJECT_NUMBER}.${REGION}.run.app"
NEW_RUN_URL="https://jquants-mcp-${PROJECT_NUMBER}.${REGION}.run.app"
NEW_BASE_URL="https://${CUSTOM_DOMAIN}"

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/providers/github"
GITHUB_REPO="shigechika/jquants-mcp"

DNS_ZONE="aikawa-jp"  # confirm with: gcloud dns managed-zones list
```

## Pre-flight checks

- [ ] `gcloud auth login shige@aikawa.jp` (user account, not the dns-updater SA)
- [ ] `gcloud config set project ${PROJECT_ID}`
- [ ] Snapshot current state (paste outputs into the runbook for ref):
  - `gcloud run services describe "${OLD_SERVICE}" --region "${REGION}" --format=yaml > /tmp/old-service.yaml`
  - `gcloud iam service-accounts get-iam-policy "${OLD_SA}" > /tmp/old-sa-iam.yaml`
  - `gcloud projects get-iam-policy "${PROJECT_ID}" --filter="bindings.members:serviceAccount:${OLD_SA}" > /tmp/old-sa-roles.yaml`
- [ ] Confirm `gcloud dns managed-zones list` shows the zone for `aikawa.jp`
- [ ] Confirm jpx-short-report side is **aware** of the rename (publisher GCS_BUCKET will need updating; coordinate timing)

## Phase 1 — Additive: new SA + bucket + WIF binding

Pure additive, no production impact. Easy rollback (delete the new resources).

### 1.1 Create the new SA

```bash
gcloud iam service-accounts create "${NEW_SERVICE}" \
  --display-name "jquants-mcp Cloud Run SA"
```

- [ ] Verify: `gcloud iam service-accounts describe "${NEW_SA}"` returns metadata.

### 1.2 Grant project-level roles to the new SA

```bash
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${NEW_SA}" --role "roles/datastore.user"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${NEW_SA}" --role "roles/secretmanager.secretAccessor"
```

- [ ] Verify: `gcloud projects get-iam-policy "${PROJECT_ID}" --flatten="bindings[].members" --filter="bindings.members:serviceAccount:${NEW_SA}"` lists both roles.

### 1.3 Add WIF impersonation binding for the new SA

```bash
gcloud iam service-accounts add-iam-policy-binding "${NEW_SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/${GITHUB_REPO}"
```

- [ ] Verify: `gcloud iam service-accounts get-iam-policy "${NEW_SA}"` shows the principalSet binding.

### 1.4 Create the new bucket

```bash
gcloud storage buckets create "gs://${NEW_BUCKET}" \
  --location "${REGION}" --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://${NEW_BUCKET}" \
  --member "serviceAccount:${NEW_SA}" --role "roles/storage.objectViewer"
```

- [ ] Verify: `gcloud storage buckets describe "gs://${NEW_BUCKET}"` exists.
- [ ] Verify: bucket IAM lists `${NEW_SA}` with `roles/storage.objectViewer`.

### 1.5 Copy objects from old → new bucket

```bash
gcloud storage cp -r "gs://${OLD_BUCKET}/*" "gs://${NEW_BUCKET}/"
```

`cache.db` is ~2.7 GB; expect 1–3 minutes intra-region.

- [ ] Verify object parity:
  ```bash
  diff <(gcloud storage ls -r "gs://${OLD_BUCKET}/**" | sed "s|${OLD_BUCKET}|BUCKET|") \
       <(gcloud storage ls -r "gs://${NEW_BUCKET}/**" | sed "s|${NEW_BUCKET}|BUCKET|")
  ```
  Empty diff = identical contents.
- [ ] Verify cache.db size matches: `gcloud storage du gs://${OLD_BUCKET}/jquants-mcp/cache.db gs://${NEW_BUCKET}/jquants-mcp/cache.db` shows the same byte count.

**Rollback for Phase 1:** delete the new resources in reverse order — none are referenced by anything yet.

## Phase 2 — Update GitHub Actions secret

### 2.1 Rotate `WIF_SERVICE_ACCOUNT`

```bash
echo -n "${NEW_SA}" | gh secret set WIF_SERVICE_ACCOUNT
```

- [ ] Verify: `gh secret list | grep WIF_SERVICE_ACCOUNT` shows the recent update timestamp.

**Rollback for Phase 2:** `echo -n "${OLD_SA}" | gh secret set WIF_SERVICE_ACCOUNT`

## Phase 3 — Update cd.yml + first deploy of the new service

### 3.1 Edit `.github/workflows/cd.yml`

Replace `jquants-dat-mcp` everywhere it appears as the service name, the bucket env, and the OAuth base URL. The IAM-bound SA is now `${NEW_SA}` via the GitHub secret already updated in Phase 2.

Diff to apply (will be done as a normal PR — see `docs/rename-runbook` branch):
- `gcloud run deploy jquants-dat-mcp` → `gcloud run deploy jquants-mcp`
- `GCS_BUCKET=aikawa-dx-jquants-dat-mcp` → `GCS_BUCKET=aikawa-dx-jquants-mcp`
- `OAUTH_BASE_URL=https://jquants-dat-mcp-...run.app` → `OAUTH_BASE_URL=https://jquants-mcp.aikawa.jp` *(after Phase 4-5 land)*
- Add `--service-account ${NEW_SA}` if not already inferred

> ⚠ Set `OAUTH_BASE_URL` to the new custom-domain URL **only after** the Google OAuth Client has the new redirect URI registered (Phase 5.1) and the cert is provisioned (Phase 4.3). Otherwise OAuth flow hits `redirect_uri_mismatch`. For the **first** deploy of the new service we keep the old run.app `OAUTH_BASE_URL` so login still works during cutover.

### 3.2 Trigger CD

Either push the cd.yml change to main, or `gh workflow run cd.yml`. Watch the run; first build creates the new Cloud Run service `jquants-mcp` from `--source .`.

- [ ] Verify: `gcloud run services describe "${NEW_SERVICE}" --region "${REGION}" --format="value(status.url)"` returns `${NEW_RUN_URL}` (or close — exact hash assigned by GCP).
- [ ] Tail logs: `gcloud run services logs read "${NEW_SERVICE}" --region "${REGION}" --limit=50` shows OAuth init + cache.db download + MCP server start.
- [ ] Smoke probe (auth-required → expect 401):
  ```bash
  curl -i -s -o /dev/null -w "%{http_code}\n" "${NEW_RUN_URL}/mcp"
  # → 401
  ```

**Rollback for Phase 3:** revert cd.yml on main and push. The new service stays alive but stops getting redeploys; old service remains in service.

## Phase 4 — Custom domain mapping

### 4.1 Create the domain mapping

```bash
gcloud beta run domain-mappings create \
  --service="${NEW_SERVICE}" \
  --domain="${CUSTOM_DOMAIN}" \
  --region="${REGION}"
```

### 4.2 Read DNS records

```bash
gcloud beta run domain-mappings describe \
  --domain="${CUSTOM_DOMAIN}" \
  --region="${REGION}" \
  --format="yaml(status.resourceRecords)"
```

Expected: a single CNAME `jquants-mcp` → `ghs.googlehosted.com.`

### 4.3 Add to Cloud DNS

```bash
gcloud dns record-sets create "${CUSTOM_DOMAIN}." \
  --zone="${DNS_ZONE}" \
  --type=CNAME --ttl=300 \
  --rrdatas="ghs.googlehosted.com."
```

- [ ] Verify resolution: `dig +short CNAME ${CUSTOM_DOMAIN}` returns `ghs.googlehosted.com.`
- [ ] Wait for cert (5–60 min): `gcloud beta run domain-mappings describe --domain="${CUSTOM_DOMAIN}" --region="${REGION}" --format="value(status.conditions[0].type,status.conditions[0].status)"` shows `Ready: True`
- [ ] HTTPS smoke probe: `curl -i -s -o /dev/null -w "%{http_code}\n" "https://${CUSTOM_DOMAIN}/mcp"` returns 401 (auth required = endpoint reachable, TLS valid)

**Rollback for Phase 4:** delete the DNS record, then `gcloud beta run domain-mappings delete --domain="${CUSTOM_DOMAIN}" --region="${REGION}"`.

## Phase 5 — OAuth Client redirect URI

### 5.1 Add the new URI in Google Cloud Console (manual)

1. Open https://console.cloud.google.com/apis/credentials?project=${PROJECT_ID}
2. Edit the OAuth 2.0 Client (the one whose ID is in GitHub Secret `GOOGLE_CLIENT_ID`)
3. Under **Authorized redirect URIs**, **add** (do NOT remove the old one yet):
   - `https://jquants-mcp.aikawa.jp/oauth/callback`
4. Save

### 5.2 (Optional) GitHub OAuth Client

If a GitHub OAuth App is also configured, repeat in https://github.com/settings/developers — add `https://jquants-mcp.aikawa.jp/oauth/callback` as an additional callback URL.

- [ ] Confirm new URI is listed.
- [ ] Old URI is still listed (keeps working during transition).

**Rollback for Phase 5:** remove the new URI in the Console.

## Phase 6 — Switch `OAUTH_BASE_URL` to the custom domain

### 6.1 Edit cd.yml again

Now that the cert is live and the OAuth Client accepts the new URI:

- `OAUTH_BASE_URL=https://jquants-mcp.aikawa.jp` (replacing the old run.app value)

Push → CD redeploys. The new service revision will issue OAuth login redirects pointing at the custom domain.

- [ ] Verify: visit `https://jquants-mcp.aikawa.jp/settings` in a private browser tab, complete OAuth, land back at `/settings` with a valid session.

**Rollback for Phase 6:** revert cd.yml; CD redeploys the previous revision.

## Phase 7 — Update README / docs / repo references

### 7.1 Replace URLs in tracked files

Files known to reference the old run.app URL or service name:
- `README.md` / `README.ja.md` — `mcp-stdio --oauth ...` examples, Cloud Run URL prose
- `docs/deploy/gcp.md` — examples (the placeholder is generic but the smoke-test URL was hard-coded for shige's instance)
- `ops/alerts/*.yaml` — `resource.labels.service_name="jquants-dat-mcp"` filters → `"jquants-mcp"`
- `scripts/collect_metrics.py` — `JQUANTS_CLOUD_RUN_SERVICE` default
- Memory: `MEMORY.md`, `project_public_release_epic.md` — many references

### 7.2 Update memory after the operational steps land

(Done as a separate commit so chat-side memory stays in sync. See "Post-cutover memory updates" below.)

## Phase 8 — Update self (Claude Code) and shige's clients

- [ ] Re-add Claude Code MCP entry on shige's machine:
  ```bash
  claude mcp remove jquants-cloud
  claude mcp add jquants-cloud -- mcp-stdio --oauth https://jquants-mcp.aikawa.jp/mcp
  ```
- [ ] Claude mobile (iOS/Android): delete the old connector, add a new one pointing at `https://jquants-mcp.aikawa.jp/mcp`. OAuth-sign in.
- [ ] Verify a tool call (`cache_status`) returns successfully via each client.

## Phase 9 — Coordinate publisher cutover (jpx-short-report)

The daily.sh on m1.local writes `cache.db` to `gs://${OLD_BUCKET}/jquants-mcp/cache.db`. After the next daily run we want it writing to `gs://${NEW_BUCKET}/jquants-mcp/cache.db`.

- [ ] Coordinate with the jpx-short-report side Claude session: send a PING with the new `GCS_BUCKET=aikawa-dx-jquants-mcp` value and ask them to update their daily.sh env.
- [ ] After the next daily run lands in the new bucket, mark complete.
- [ ] Old bucket continues to receive the old prefix from any not-yet-cut publisher; the new Cloud Run service ignores the old bucket entirely.

## Phase 10 — Observation period (≥ 2 days)

Watch for issues before destructive cleanup:

- [ ] Cloud Run logs clean (no 5xx spike, OAuth flow rate normal)
- [ ] daily.sh runs land in the new bucket (Phase 9 confirmed)
- [ ] No alerting policy fires due to mis-mapped service-name filters

## Phase 11 — Cleanup (destructive — confirm before each step)

> Each step below is irreversible-ish (delete + recreate possible but expensive). Confirm one-by-one with shige.

### 11.1 Delete old Cloud Run service

```bash
gcloud run services delete "${OLD_SERVICE}" --region "${REGION}"
```

### 11.2 Remove old WIF binding

```bash
gcloud iam service-accounts remove-iam-policy-binding "${OLD_SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/${GITHUB_REPO}"
```

### 11.3 Remove old SA's project-level roles

```bash
for ROLE in roles/datastore.user roles/secretmanager.secretAccessor; do
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${OLD_SA}" --role "${ROLE}"
done
```

### 11.4 Delete old SA

```bash
gcloud iam service-accounts delete "${OLD_SA}"
```

### 11.5 Delete old bucket

```bash
# Only after Phase 9 confirms the publisher is on the new bucket and old bucket has no recent writes.
gcloud storage rm -r "gs://${OLD_BUCKET}/"   # delete contents
gcloud storage buckets delete "gs://${OLD_BUCKET}"
```

### 11.6 Remove old OAuth redirect URI

In the Google Cloud Console, remove the old run.app URI from the OAuth Client.

## Post-cutover memory updates

Once Phase 11 is done:

- `MEMORY.md`: replace all `jquants-dat-mcp` infra references with `jquants-mcp`.
- `project_public_release_epic.md`: mark the rename-cleanup section done; remove the "Option C" caveat.
- Note v0.x release version that contains the cd.yml change.

## Quick verification commands (anytime)

```bash
# Cloud Run service status
gcloud run services describe "${NEW_SERVICE}" --region "${REGION}" \
  --format="value(metadata.name,status.url,status.latestReadyRevisionName)"

# Custom domain readiness
gcloud beta run domain-mappings describe --domain="${CUSTOM_DOMAIN}" --region="${REGION}" \
  --format="value(status.conditions[].type,status.conditions[].status)"

# OAuth round-trip via Claude Code (after re-add)
claude mcp call jquants-cloud cache_status
```

## When to use this runbook again

Anytime a similar Cloud Run + SA + GCS rename comes up (e.g. another project's deployment). The structure (additive → cd.yml → domain → OAuth → observation → cleanup) generalizes.

Reference: companion to `#106` (custom domain) and `#105` (rename, Option C originally) — see `project_public_release_epic.md` for the trail.
