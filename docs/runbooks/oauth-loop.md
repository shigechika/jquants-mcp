# OAuth loop / persistent 401

## Symptom

- Users report "Authorization with the MCP server failed" in Claude Desktop
- Users loop between the consent screen and the app, never completing login
- `/settings` page inaccessible (401)

## Quick check

```sh
# POST /token and userinfo status in the last hour
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-mcp"
   (textPayload:"POST /token" OR textPayload:"/oauth/callback")' \
  --project=${PROJECT} --limit=30 --freshness=1h --format=json \
  | jq -r '.[] | "\(.timestamp) \(.httpRequest.status) \(.httpRequest.requestUrl // .textPayload)"'

# oauth_state collection size
gcloud firestore documents list --database='(default)' \
  --collection=oauth_state --limit=5 --project=${PROJECT}

# Secrets present
gcloud secrets versions access latest --secret=OAUTH_JWT_SIGNING_KEY --project=${PROJECT} | head -c 20
gcloud secrets versions access latest --secret=google-oauth-client-secret --project=${PROJECT} | head -c 20
```

## Root cause options

1. **Claude Desktop bug #40102** — server side succeeds (POST /token → 200,
   Google userinfo → 200) but Desktop fails to save the token. Not a server
   issue. Workaround: route the affected user to `mcp-stdio --oauth`.
2. **JWT signing key rotated without dual-key window** — all existing
   sessions become invalid. See #82 once implemented.
3. **Stale `oauth_state` entries** blocking new flows due to CSRF mismatch.
4. **Google OAuth client secret expired or revoked** in GCP console.

## Recovery

- **Desktop bug**: nothing to fix server-side. Advise user to use mcp-stdio
  (`claude mcp add jquants-cloud -- mcp-stdio --oauth <URL>`).
- **Signing key mismatch**: roll forward a valid key; users must re-login.
- **Stale oauth_state**: clear old entries (> 1 hour old):
  ```sh
  # via Firestore console UI, or a one-off script; no built-in tool yet
  ```
- **Client secret**: rotate in GCP → Secret Manager → redeploy via CD.

## Post-incident

- If Desktop bug is suspected, confirm via Cloud Run logs showing successful
  POST /token. Note the user-facing workaround in the session.
- If a signing key rotation was the cause, fast-track #82 (dual-key support).
