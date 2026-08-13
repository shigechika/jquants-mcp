# Cloud Run Deployment Guide (GCP)

Deploy your own instance of jquants-mcp to Google Cloud Run with OAuth 2.1 login, per-user encrypted J-Quants API keys, and Claude Desktop / Claude mobile compatibility.

This is a long guide because Cloud Run multi-user deployment has real moving parts. Budget ~2–4 hours the first time, mostly spent waiting for DNS / TLS.

## Architecture

`jquants-mcp` speaks **stdio only** — it has no HTTP surface of its own. On Cloud Run it runs behind two layers, in one service made of two containers:

1. **`oauth2-proxy` (ingress container)** — terminates user sign-in for the service. It skip-auths `^/mcp(/|$)`, because the MCP path has its own OAuth gate below.
2. **`mcp-stdio serve` (app container)** — the MCP gateway, started by [`scripts/entrypoint-stdio.sh`](../../scripts/entrypoint-stdio.sh). It terminates MCP OAuth 2.1 on `/mcp`, and spawns one `jquants-mcp` child process per authenticated user, injecting that user's identity from the `X-Forwarded-Email` header into the child as the `JQUANTS_MCP_USER` environment variable.

State is split across managed stores so instances stay stateless:

- **`cache.db`** (market data) — published to a GCS bucket by a self-hosted publisher, downloaded to `/tmp` **synchronously at container start**. Cloud Run reads, never writes back, and never refreshes it in place: an instance serves the snapshot it started with until it is replaced.
- **`users`** (per-user encrypted J-Quants API keys) — Firestore `users` collection, written by the `register_api_key` MCP tool.
- **OAuth tokens** — Firestore, in the document configured by `FIRESTORE_TOKEN_STORE` (default `mcp_stdio_oauth/state`). Owned by `mcp-stdio serve`, not by this package.
- **Secrets** (encryption key, allowlist, J-Quants fallback key) — Google Secret Manager.

> **Scope of this guide.** The GCP project, GCS bucket, Firestore, WIF and CD wiring below are all reproducible from this repo. The two-container service definition itself (the `oauth2-proxy` sidecar, its env vars and secrets) is provisioned outside this repo — `cd.yml` only ever updates the **app** container's image on an already-existing service. See [step 12](#12-deploy).

## Estimated cost

At < 1000 requests/day:

| Service | Cost |
|---|---|
| Cloud Run | $0 (free tier covers typical personal usage) |
| Firestore | $0 (free tier: 50k reads + 20k writes/day) |
| GCS | ~$0.07/mo (3 GiB, us-west1) |
| Secret Manager | ~$0.18/mo (3 secrets × $0.06) |
| Cloud DNS | $0.20/mo per hosted zone (if custom domain) |
| **Total** | **< $1/mo** for personal / family use |

Heavier traffic scales roughly linearly with Cloud Run's request pricing. See [docs/cloud-run-memory-sizing.md](../cloud-run-memory-sizing.md) for sizing notes.

## Prerequisites

- A Google Cloud account with billing enabled
- A GCP project (will create one below if needed)
- The [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed locally
- A GitHub account (you'll fork the repo and run the CD workflow)
- A J-Quants API key (any plan — Free works)
- Optional: a domain name you control, for a custom URL like `jquants-mcp.example.com`

## 1. Fork and clone

Fork [shigechika/jquants-mcp](https://github.com/shigechika/jquants-mcp) on GitHub, then:

```bash
git clone git@github.com:YOUR_USERNAME/jquants-mcp.git
cd jquants-mcp
```

## 2. Set environment variables

These shell variables are used throughout the rest of the guide. Adjust to taste.

```bash
export PROJECT_ID="jquants-mcp-$(whoami)"   # or any unique ID
export REGION="us-west1"                      # any Cloud Run region
export SERVICE="jquants-mcp"
export GCS_BUCKET="${PROJECT_ID}-cache"       # must be globally unique
export SA_NAME="jquants-mcp"
export SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export GITHUB_REPO="YOUR_USERNAME/jquants-mcp"
```

## 3. Create and configure the GCP project

```bash
gcloud projects create "${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

# Link billing (replace with your billing account ID)
gcloud billing accounts list
gcloud billing projects link "${PROJECT_ID}" \
  --billing-account=<BILLING_ACCOUNT_ID>

# Enable required APIs
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

If you plan to use a custom domain:

```bash
gcloud services enable dns.googleapis.com
```

## 4. Create the service account

```bash
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name "jquants-mcp Cloud Run SA"

# Read-only access to the cache.db snapshot in GCS (added below once bucket exists)
# Firestore read/write
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${SA}" \
  --role "roles/datastore.user"

# Secret Manager access
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${SA}" \
  --role "roles/secretmanager.secretAccessor"
```

## 5. Create the GCS bucket

```bash
gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --location "${REGION}" \
  --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
  --member "serviceAccount:${SA}" \
  --role "roles/storage.objectViewer"
```

Disable parallel composite uploads on the publisher host — they corrupt SQLite files:

```bash
gcloud config set storage/parallel_composite_upload_enabled False
```

## 6. Enable Firestore

```bash
gcloud firestore databases create \
  --location="${REGION}" \
  --type=firestore-native
```

No schema setup needed. The `users` collection is created on the first `register_api_key` call, and `mcp-stdio serve` creates its own token-store document on the first sign-in.

## 7. Set up Workload Identity Federation (WIF)

WIF lets GitHub Actions authenticate to GCP without a long-lived service account key. The GitHub Actions OIDC token is exchanged for a short-lived GCP token, scoped to the exact repo and workflow.

```bash
# Create a Workload Identity Pool
gcloud iam workload-identity-pools create github-actions \
  --location=global \
  --display-name="GitHub Actions"

# Create a Provider inside the pool (scoped to your fork)
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global \
  --workload-identity-pool=github-actions \
  --display-name="GitHub" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${GITHUB_REPO}'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Get the Provider resource name (used as a GitHub secret later)
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
export WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/providers/github"
echo "WIF_PROVIDER=${WIF_PROVIDER}"

# Allow GitHub Actions (in your fork) to impersonate the service account
gcloud iam service-accounts add-iam-policy-binding "${SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/${GITHUB_REPO}"
```

The `attribute-condition` on the Provider is your security boundary: only workflows from `${GITHUB_REPO}` can exchange tokens. If you fork and later transfer the repo, you must update this condition.

## 8. Create the Google OAuth client for the sign-in layer

User sign-in is handled by the `oauth2-proxy` sidecar, not by `jquants-mcp`. The package holds no OAuth client credentials and exposes no callback route of its own.

1. [APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials) in the GCP console
2. Configure the OAuth consent screen (User type: External, scopes: `openid email profile`)
3. Create OAuth 2.0 Client ID → Web application
4. Authorized redirect URI: the callback path your `oauth2-proxy` configuration uses, under your service's public URL. You'll set this for real after the first deploy (the URL is assigned at deploy time). Leave a placeholder for now and come back.
5. Note the Client ID and Client Secret — these are configured **on the sidecar container**, alongside its other env vars and secrets. `cd.yml` never reads or writes them.

MCP clients authenticate separately, against `mcp-stdio serve`'s own OAuth 2.1 endpoints on `/mcp`. That layer registers clients dynamically; there is nothing to create here for it.

## 9. Populate Secret Manager

```bash
# J-Quants API key (fallback for users who have not registered their own;
# per-user keys are stored encrypted in Firestore)
echo -n "<YOUR_JQUANTS_API_KEY>" | gcloud secrets create jquants-api-key --data-file=-

# Random 32-byte hex for per-user API key encryption (AES-256-GCM)
python3 -c "import secrets; print(secrets.token_hex(32))" | \
  tr -d '\n' | gcloud secrets create mcp-encryption-key --data-file=-

# Allowlist: comma-separated emails that are allowed to sign in.
# Empty value allows any authenticated user.
echo -n "you@example.com,family@example.com" | \
  gcloud secrets create jquants-allowed-emails --data-file=-
```

The `oauth2-proxy` sidecar's own secrets (its Google client secret and cookie secret) are managed with the sidecar, outside this guide.

To update any secret later:

```bash
echo -n "<NEW_VALUE>" | gcloud secrets versions add <SECRET_NAME> --data-file=-
```

Cloud Run services using `--set-secrets "X=SECRET:latest"` pick up the new version on next deploy (or on next cold start, depending on how `gcloud run services update` is invoked — see below).

## 10. Add GitHub Actions secrets and variables

In your fork, go to **Settings → Secrets and variables → Actions** and add:

**Secrets** (encrypted):

| Secret | Value |
|---|---|
| `WIF_PROVIDER` | The `${WIF_PROVIDER}` value printed in step 7 |
| `WIF_SERVICE_ACCOUNT` | `${SA}` (the full email) |

**Variables** (plain text, visible in logs):

| Variable | Example | Description |
|---|---|---|
| `GCP_PROJECT` | `my-gcp-project` | GCP project ID |
| `GCP_REGION` | `us-west1` | Cloud Run region |
| `GCP_SERVICE_ACCOUNT` | `jquants-mcp@my-gcp-project.iam.gserviceaccount.com` | Runtime service account |
| `GCS_BUCKET` | `my-gcp-project-jquants-mcp` | GCS bucket for `cache.db` |
| `OAUTH_BASE_URL` | `https://your-domain.example.com` | Public base URL of the service. CD builds its post-deploy smoke-test URL from it (`${OAUTH_BASE_URL}/mcp`) |
| `CLOUDRUN_SERVICE` | whatever you set `$SERVICE` to in step 2 | Cloud Run service name. Must match the service you actually created, or CD updates a service that does not exist and fails with `NOT_FOUND` |

You can set them all at once with the `gh` CLI:

```bash
gh variable set GCP_PROJECT        --body "my-gcp-project"
gh variable set GCP_REGION         --body "us-west1"
gh variable set GCP_SERVICE_ACCOUNT --body "jquants-mcp@my-gcp-project.iam.gserviceaccount.com"
gh variable set GCS_BUCKET         --body "my-gcp-project-jquants-mcp"
gh variable set OAUTH_BASE_URL     --body "https://your-domain.example.com"
gh variable set CLOUDRUN_SERVICE   --body "${SERVICE}"
```

> **`OAUTH_BASE_URL`**: the final public URL of your service, with no trailing slash. If you don't have a custom domain yet, deploy once, note the `*.run.app` URL, set `OAUTH_BASE_URL` and the app container's `PUBLIC_URL` to it, update the sign-in layer's redirect URI from step 8, then redeploy. Despite the name it is not consumed by any OAuth code in this package — it is the base URL CD probes after a deploy.

## 11. Publish an initial `cache.db`

Cloud Run reads `cache.db` read-only. Populate it from your own machine first:

```bash
# On your workstation
uv run jquants-mcp            # once, to create ~/.cache/jquants-mcp/cache.db
uv run scripts/daily_fetch.py # or scripts/bulk_fetch_all.py for historical data

# Upload
gcloud storage cp ~/.cache/jquants-mcp/cache.db \
  "gs://${GCS_BUCKET}/jquants-mcp/cache.db" \
  --no-gzip-in-flight
```

Keep a cron / launchd job running `daily_fetch.py + gcs_export_cache.py` on your workstation so Cloud Run always has a fresh snapshot. See [local.md](local.md) for the cache-population commands.

Chain `verify_cache_completeness.py` after `daily_fetch.py` (`daily_fetch.py && verify_cache_completeness.py`) so an incomplete or stale cache surfaces a non-zero exit in the publisher's logs before the snapshot is exported. The bundled `scripts/daily-fetch.crontab` already does this for the container path.

## 12. Deploy

> **CD updates an image, it does not create the service.** The deploy step runs
> `gcloud run services update --container app --image …`, which only replaces the
> **app** container's image on a service that already exists. The two-container
> definition — the `oauth2-proxy` sidecar, the app container's `PUBLIC_URL` /
> `GCS_BUCKET` / secret bindings, and the `--command` selecting
> `scripts/entrypoint-stdio.sh` — must be created once, out of band, before the
> first CD run. That is deliberate: scaling and CPU settings on this service are
> billing-relevant and load-bearing (`min-instances=0`, CPU always allocated), and
> CD asserts them rather than setting them, so an out-of-band change fails the
> deploy instead of being silently overwritten.

Once the service exists, trigger a deploy manually from the **Actions** tab → **CD** → **Run workflow**. Watch the logs; first build takes 5–10 minutes (later deploys are faster thanks to Cloud Build layer cache).

After success:

```bash
gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format="value(status.url)"
```

Note the URL, e.g. `https://jquants-mcp-abc123-uw.a.run.app`.

Update in this order:
1. Set the `OAUTH_BASE_URL` repo Variable to this URL: `gh variable set OAUTH_BASE_URL --body "<URL>"`
2. Set the app container's `PUBLIC_URL` env var to the same URL — `mcp-stdio serve` advertises its OAuth metadata from it
3. Update the sign-in layer's redirect URI (step 8) to the callback path under this URL
4. Re-run the CD workflow (`gh workflow run cd.yml`) to pick up the new variable

## 13. Smoke test

There is no plain HTTP health endpoint: `oauth2-proxy` skip-auths `^/mcp(/|$)`, and `mcp-stdio serve`'s own OAuth layer gates it, so an **unauthenticated `POST /mcp` returning 401 is the success case**. A 5xx (or a connection failure) means the revision is unhealthy.

```bash
URL=$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format="value(status.url)")

# 1. The MCP endpoint is serving (expect 401 Unauthorized)
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${URL}/mcp"

# 2. Confirm the gateway started
gcloud run services logs read "${SERVICE}" --region "${REGION}" --limit=50 \
  | grep -E "Starting mcp-stdio serve|starting \(transport=stdio\)"
```

Only the first line is expected on a fresh revision. `starting (transport=stdio)` is logged by a `jquants-mcp` child process, and the gateway spawns one **per authenticated user** — so it appears after the first sign-in, not at startup. Its absence on an idle revision is normal.

Retry check 1 for a minute or two after a deploy. Cloud Run's readiness is driven by the ingress (`oauth2-proxy`) container, so traffic can cut over while the app container is still running its synchronous `cache.db` download; `oauth2-proxy` returns 502 for that window. CD's own verification step retries for exactly this reason.

Full functional validation comes from connecting a Claude client — see [step 15](#15-connect-from-claude-clients).

## 14. Custom domain (optional)

### Cloud DNS

Create a zone for your domain (or use an existing one):

```bash
gcloud dns managed-zones create example-com \
  --description="example.com" \
  --dns-name="example.com." \
  --visibility=public
```

At your registrar, update the NS records to the 4 nameservers printed by:

```bash
gcloud dns managed-zones describe example-com --format="value(nameServers)"
```

### Domain mapping

```bash
gcloud beta run domain-mappings create \
  --service="${SERVICE}" \
  --domain="jquants-mcp.example.com" \
  --region="${REGION}"

# Read the required DNS records
gcloud beta run domain-mappings describe \
  --domain="jquants-mcp.example.com" \
  --region="${REGION}" \
  --format="yaml(status.resourceRecords)"
```

Add the returned CNAME (or A/AAAA) to Cloud DNS:

```bash
gcloud dns record-sets create jquants-mcp.example.com. \
  --zone=example-com \
  --type=CNAME \
  --ttl=300 \
  --rrdatas="ghs.googlehosted.com."
```

Cloud Run provisions a TLS cert automatically. DNS + cert propagation takes 15–60 minutes.

Once the domain works, update `OAUTH_BASE_URL`, the app container's `PUBLIC_URL`, and the sign-in layer's redirect URI to the custom domain. Redeploy.

## 15. Connect from Claude clients

### Registering your J-Quants API key

There is no web page for this — the settings UI was removed in 1.0.0. Each user registers their own key by asking Claude to call the **`register_api_key`** MCP tool in a normal chat, once the connector is signed in:

```text
Call register_api_key with api_key="<your J-Quants API key>"
# → {"status": "ok", "plan": "light", ...}
```

The key is encrypted with `MCP_ENCRYPTION_KEY` and stored against your identity in the Firestore `users` collection. Verify with `health_check()`.

### Claude Desktop (Connectors UI)

1. Settings → Connectors → Add custom connector
2. URL: `https://jquants-mcp.example.com/mcp` (or the Cloud Run URL)
3. Sign in with Google when prompted — the first sign-in creates a user record in Firestore
4. Register your J-Quants API key with the `register_api_key` tool (above)

### Claude mobile (iOS / Android)

Verified working as of 2026-04-23 (Sonnet 4.6).

1. Open the app, go to **Settings → Connectors → Add**
2. Enter the same URL as Claude Desktop
3. Sign in with Google
4. Register your J-Quants API key with the `register_api_key` tool (above)

### Claude Code (via mcp-stdio)

Claude Code currently has a bug that drops the `Authorization` header on HTTP transports and does not play nicely with Cloud Run's OAuth flow. Use [mcp-stdio](https://pypi.org/project/mcp-stdio/) as a proxy:

```bash
claude mcp add jquants-mcp \
  -- uvx mcp-stdio --oauth https://jquants-mcp.example.com/mcp
```

`mcp-stdio --oauth` drives the OAuth 2.1 flow in your browser and caches the token locally.

## 16. Allowlist customization

The `JQUANTS_ALLOWED_EMAILS` secret controls who can sign in.

| Intent | Value |
|---|---|
| Only you | `you@example.com` |
| You + family | `you@example.com,family1@example.com,family2@example.com` |
| Any authenticated user | (empty) — the Google OAuth consent screen is your only gate |

To update:

```bash
echo -n "you@example.com,family@example.com" | \
  gcloud secrets versions add jquants-allowed-emails --data-file=-
# Trigger a redeploy so the new version is picked up
gh workflow run cd.yml
```

## 17. Monitoring and alerts

The repo ships with alert policies in [`ops/alerts/`](../../ops/alerts/). They expect a notification channel called `ops-email`:

```bash
gcloud alpha monitoring channels create \
  --display-name="ops-email" \
  --type=email \
  --channel-labels=email_address="you@example.com"

# Grab the channel ID from:
gcloud alpha monitoring channels list --format="value(name)"

CHANNEL="projects/${PROJECT_ID}/notificationChannels/<ID>" ./ops/alerts/deploy.sh
```

`deploy.sh` substitutes the channel and reconciles by `displayName`, so
re-running it updates the existing policies instead of creating duplicates.

Two things to check before you run it:

- **The service name in the YAML must match `$SERVICE`.** The policies ship
  filtering on the name this project deploys under. If yours differs, edit
  `resource.labels.service_name` (and the MQL filter in `02-5xx-rate.yaml`)
  first — a filter naming a service that does not exist is syntactically valid
  and silently matches nothing.
- **Re-run `deploy.sh` after any rename.** Renaming the service in the repo
  changes nothing in Cloud Monitoring until the policies are pushed again.
  This repo left four months of alerts pointing at a superseded name that way,
  and the failure is invisible: the policies stay green because nothing ever
  matches their filter.

After deploying, confirm the filters resolve against real data rather than
just parsing:

```bash
gcloud alpha monitoring policies list --format=json \
  | grep -o 'service_name[^,]*'   # every hit should name $SERVICE
```

## 18. Upgrade (keep your fork in sync)

Occasionally pull upstream changes:

```bash
git remote add upstream https://github.com/shigechika/jquants-mcp.git  # once
git fetch upstream
git merge upstream/main
# Resolve any conflicts in cd.yml (you edited SERVICE / PROJECT_ID / URLs)
git push origin main
```

CI runs on push; if it passes, CD deploys automatically. Roll back via Cloud Run revisions if needed:

```bash
gcloud run services update-traffic "${SERVICE}" --region "${REGION}" \
  --to-revisions=<previous-revision>=100
```

## Troubleshooting

### Deploy fails with `PERMISSION_DENIED` from WIF

Verify the Provider's attribute condition matches your repo path exactly (including username case):

```bash
gcloud iam workload-identity-pools providers describe github \
  --workload-identity-pool=github-actions \
  --location=global
```

If you renamed or transferred the repo, update `--attribute-condition` to the new path.

### Cloud Run 503 / healthcheck fails

Check logs:

```bash
gcloud run services logs read "${SERVICE}" --region "${REGION}" --limit=100
```

Common causes:
- `cache.db` not yet downloaded from GCS → the app container downloads it synchronously at startup; wait for that to finish (`oauth2-proxy` answers 502 meanwhile), or confirm the object exists in the bucket
- Missing env var / secret on the app container → check the service definition
- Sign-in misconfiguration → verify the app container's `PUBLIC_URL` matches the service's real public URL, and that the sign-in layer's redirect URI matches it too

### `cache_status` returns minimal payload (no row counts)

Background `cache.db` download hasn't finished yet. See the runbook: [cache-db-missing](../runbooks/cache-db-missing.md).

### OAuth loop or sign-in fails

See [oauth-loop](../runbooks/oauth-loop.md).

### Firestore permission errors

Verify the SA has `roles/datastore.user`:

```bash
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${SA}"
```

### More

See [`docs/runbooks/`](../runbooks/) for incident-response playbooks.
