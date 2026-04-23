# Cloud Run Deployment Guide (GCP)

Deploy your own instance of jquants-mcp to Google Cloud Run with OAuth 2.1 login, per-user encrypted J-Quants API keys, and Claude Desktop / Claude mobile compatibility.

This is a long guide because Cloud Run multi-user deployment has real moving parts. Budget ~2–4 hours the first time, mostly spent waiting for DNS / TLS.

## Architecture

Cloud Run runs the jquants-mcp HTTPS server; state is split across managed stores so instances can scale horizontally:

- **`cache.db`** (market data) — published to a GCS bucket by a self-hosted publisher, downloaded to `/tmp` on Cloud Run cold start. Cloud Run reads, never writes back.
- **`users`** (per-user encrypted J-Quants API keys) — Firestore `users` collection.
- **`oauth_state`** (OAuth sessions, PKCE verifiers, dynamic client registrations) — Firestore `oauth_state` collection.
- **Secrets** (OAuth client secrets, encryption key, allowlist) — Google Secret Manager.

## Estimated cost

At < 1000 requests/day:

| Service | Cost |
|---|---|
| Cloud Run | $0 (free tier covers typical personal usage) |
| Firestore | $0 (free tier: 50k reads + 20k writes/day) |
| GCS | ~$0.07/mo (3 GiB, us-west1) |
| Secret Manager | ~$0.30/mo (6 secrets × $0.06) |
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

No schema setup needed. The server creates `users` and `oauth_state` collections on first write.

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

## 8. Create OAuth clients

### Google OAuth (required for Cloud Run)

1. [APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials) in the GCP console
2. Configure the OAuth consent screen (User type: External, scopes: `openid email profile`)
3. Create OAuth 2.0 Client ID → Web application
4. Authorized redirect URI: `https://<your Cloud Run URL>/oauth/callback` — you'll set this for real after the first deploy (the URL is assigned at deploy time). Leave a placeholder for now and come back.
5. Note the Client ID and Client Secret

### GitHub OAuth (optional, legacy path)

1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
2. Authorization callback URL: `https://<your Cloud Run URL>/oauth/callback`
3. Note the Client ID and Client Secret

## 9. Populate Secret Manager

```bash
# J-Quants API key (fallback for anonymous / pre-login users; per-user keys stored in Firestore)
echo -n "<YOUR_JQUANTS_API_KEY>" | gcloud secrets create jquants-api-key --data-file=-

# Google OAuth client secret
echo -n "<GOOGLE_OAUTH_CLIENT_SECRET>" | gcloud secrets create google-oauth-client-secret --data-file=-

# GitHub OAuth client secret (optional; can use the same secret name placeholder)
echo -n "<GITHUB_OAUTH_CLIENT_SECRET>" | gcloud secrets create github-oauth-client-secret --data-file=-

# Random 32-byte hex for per-user API key encryption (AES-256-GCM)
python3 -c "import secrets; print(secrets.token_hex(32))" | \
  tr -d '\n' | gcloud secrets create mcp-encryption-key --data-file=-

# Random JWT signing key for OAuth session tokens
python3 -c "import secrets; print(secrets.token_urlsafe(48))" | \
  tr -d '\n' | gcloud secrets create OAUTH_JWT_SIGNING_KEY --data-file=-

# Allowlist: comma-separated emails that are allowed to sign in.
# Empty value allows any authenticated user.
echo -n "you@example.com,family@example.com" | \
  gcloud secrets create jquants-allowed-emails --data-file=-
```

To update any secret later:

```bash
echo -n "<NEW_VALUE>" | gcloud secrets versions add <SECRET_NAME> --data-file=-
```

Cloud Run services using `--set-secrets "X=SECRET:latest"` pick up the new version on next deploy (or on next cold start, depending on how `gcloud run services update` is invoked — see below).

## 10. Add GitHub Actions secrets

In your fork, go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `WIF_PROVIDER` | The `${WIF_PROVIDER}` value printed in step 7 |
| `WIF_SERVICE_ACCOUNT` | `${SA}` (the full email) |
| `GOOGLE_CLIENT_ID` | Google OAuth Client ID from step 8 |
| `GH_OAUTH_CLIENT_ID` | GitHub OAuth Client ID from step 8 |

## 11. Adjust the CD workflow

The included [`.github/workflows/cd.yml`](../../.github/workflows/cd.yml) is wired to the upstream project (`PROJECT_ID=aikawa-dx`, `SERVICE=jquants-dat-mcp`). Edit the `gcloud run deploy` line in your fork to match your environment:

```yaml
gcloud run deploy ${SERVICE} \
  --project ${PROJECT_ID} \
  --region ${REGION} \
  --source . \
  --execution-environment gen2 \
  --memory 4Gi \
  --cpu 1 \
  --no-cpu-throttling \
  --cpu-boost \
  --clear-volumes --clear-volume-mounts \
  --set-env-vars "..." \
  --set-secrets "..."
```

Replace the hard-coded `OAUTH_BASE_URL` with your future Cloud Run URL (you'll know it after the first deploy — place a temporary value, redeploy once to learn the URL, then update `OAUTH_BASE_URL` and the OAuth redirect URIs, then redeploy again).

Commit the changes to `main` on your fork.

## 12. Publish an initial `cache.db`

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

Keep a cron / launchd job running `daily_fetch.py + gcs_export_cache.py` on your workstation so Cloud Run always has a fresh snapshot. See [local.md](local.md) for the publisher pattern.

## 13. Deploy

Trigger the first deploy manually from the **Actions** tab → **CD** → **Run workflow**. Watch the logs; first build takes 5–10 minutes (later deploys are faster thanks to Cloud Build layer cache).

After success:

```bash
gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format="value(status.url)"
```

Note the URL, e.g. `https://jquants-mcp-abc123-uw.a.run.app`.

Update in this order:
1. Set `OAUTH_BASE_URL` in `cd.yml` to this URL
2. Update the OAuth client redirect URIs (Google + GitHub) to `<URL>/oauth/callback`
3. Commit + push → CD redeploys automatically

## 14. Smoke test

The server does not expose a plain HTTP health endpoint — authentication is required, and the MCP protocol expects a POST handshake. Three checks:

```bash
URL=$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format="value(status.url)")

# 1. Cloud Run is serving (expect 401 Unauthorized, which proves the
#    server started and OAuth is enforced)
curl -i -s -o /dev/null -w "%{http_code}\n" "${URL}/mcp"

# 2. /settings returns the API key registration page (redirects to OAuth)
curl -i -s -o /dev/null -w "%{http_code}\n" "${URL}/settings"

# 3. Tail the logs and confirm the startup banner
gcloud run services logs read "${SERVICE}" --region "${REGION}" --limit=50 \
  | grep -E "SIGHUP handler installed|Initializing .* OAuth"
```

Full functional validation comes from connecting a Claude client — see [step 16](#16-connect-from-claude-clients).

## 15. Custom domain (optional)

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

Once the domain works, update `OAUTH_BASE_URL` and the OAuth redirect URIs to the custom domain. Redeploy.

## 16. Connect from Claude clients

### Claude Desktop (Connectors UI)

1. Settings → Connectors → Add custom connector
2. URL: `https://jquants-mcp.example.com/mcp` (or the Cloud Run URL)
3. Sign in with Google when prompted — the first sign-in creates a user record in Firestore
4. Go to the jquants-mcp `/settings` web page (it's linked from the connector panel) and register your J-Quants API key

### Claude mobile (iOS / Android)

Verified working as of 2026-04-23 (Sonnet 4.6).

1. Open the app, go to **Settings → Connectors → Add**
2. Enter the same URL as Claude Desktop
3. Sign in with Google
4. Register your J-Quants API key via the `/settings` web page (open it in a mobile browser tab)

### Claude Code (via mcp-stdio)

Claude Code currently has a bug that drops the `Authorization` header on HTTP transports and does not play nicely with Cloud Run's OAuth flow. Use [mcp-stdio](https://pypi.org/project/mcp-stdio/) as a proxy:

```bash
claude mcp add jquants-mcp \
  -- uvx mcp-stdio --oauth https://jquants-mcp.example.com/mcp
```

`mcp-stdio --oauth` drives the OAuth 2.1 flow in your browser and caches the token locally.

## 17. Allowlist customization

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

## 18. Monitoring and alerts

The repo ships with alert policies in [`ops/alerts/`](../../ops/alerts/). They expect a notification channel called `ops-email`:

```bash
gcloud alpha monitoring channels create \
  --display-name="ops-email" \
  --type=email \
  --channel-labels=email_address="you@example.com"

# Grab the channel ID from:
gcloud alpha monitoring channels list --format="value(name)"

# Edit ops/alerts/*.yaml to reference the channel ID, then:
for f in ops/alerts/*.yaml; do
  gcloud alpha monitoring policies create --policy-from-file="$f"
done
```

## 19. Upgrade (keep your fork in sync)

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
- `cache.db` not yet downloaded from GCS → wait 1–2 minutes after a cold start, or confirm the object exists in the bucket
- Missing env var / secret → check `cd.yml` for typos
- OAuth misconfiguration → verify `OAUTH_BASE_URL` matches the Cloud Run URL and redirect URIs match

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
