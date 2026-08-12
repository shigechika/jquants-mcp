# OAuth loop / persistent 401

## Which layer is this about?

**Not jquants-mcp.** Since 1.0.0 the package has no OAuth code, no callback
route and no session state: it is a stdio child process that receives an
already-authenticated identity in `JQUANTS_MCP_USER`. It cannot produce a 401.

Two independent OAuth layers sit in front of it on Cloud Run, and the first job
of this runbook is deciding which one is failing:

| Layer | Covers | Typical symptom |
|---|---|---|
| **`oauth2-proxy`** (ingress sidecar) | User sign-in for the service. It skip-auths `^/mcp(/\|$)`. | Browser bounces on the Google consent screen when opening the service URL |
| **`mcp-stdio serve`** (app container) | MCP OAuth 2.1 on `/mcp` — the flow a Claude client actually runs | "Authorization with the MCP server failed" in Claude Desktop / mobile; client loops between consent and app |

If the user *is* signed in and tools instead fail with "not configured" or
"invalid API key", this is not an OAuth problem — it is J-Quants key
registration. Send them to `register_api_key` (see
[plan-upgrade.md](plan-upgrade.md)).

## Quick check

```sh
# 4xx/5xx on the MCP path in the last hour — tells you whether the client
# ever reached mcp-stdio serve's OAuth layer at all
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="jquants-mcp"
   httpRequest.requestUrl:"/mcp"
   httpRequest.status>=400' \
  --project=${PROJECT} --limit=30 --freshness=1h --format=json \
  | jq -r '.[] | "\(.timestamp) \(.httpRequest.status) \(.httpRequest.requestUrl)"'

# Did the gateway start on the serving revision?
# "Starting mcp-stdio serve" is expected on every revision.
# "starting (transport=stdio)" comes from a per-user jquants-mcp child, so it
# only appears once someone has signed in successfully — its presence is
# therefore positive evidence that at least one user got through OAuth.
gcloud run services logs read ${SERVICE} --region=${REGION} --limit=50 \
  | grep -E "Starting mcp-stdio serve|starting \(transport=stdio\)"

# Gateway token store (persisted OAuth tokens; FIRESTORE_TOKEN_STORE,
# default mcp_stdio_oauth/state)
gcloud firestore documents list --database='(default)' \
  --collection=mcp_stdio_oauth --limit=5 --project=${PROJECT}
```

## Root cause options

1. **Claude Desktop bug #40102** — the server side succeeds (token issued,
   200s in the log) but Desktop fails to save the token. Not a server issue.
   Workaround: route the affected user to `mcp-stdio --oauth`.
2. **`PUBLIC_URL` mismatch** (`mcp-stdio serve`) — the gateway advertises its
   OAuth metadata from `PUBLIC_URL`. If it does not match the URL the client
   dialed (custom domain added, service URL changed, trailing slash), the
   client is redirected somewhere it cannot complete. Check the app
   container's `PUBLIC_URL` env var against the real public URL.
3. **Redirect URI not allowed** (`mcp-stdio serve`) — the gateway is started
   with an explicit `--allow-redirect-uri` list
   (`https://claude.ai/api/mcp/auth_callback` in `entrypoint-stdio.sh`). A
   client using any other callback is rejected outright.
4. **Token store unavailable** (`mcp-stdio serve`) — if the Firestore token
   store is misconfigured or unwritable, tokens fall back to being lost on
   every restart, which users experience as "it keeps asking me to log in".
   See Firestore runbooks: [firestore-outage](firestore-outage.md).
5. **Sign-in layer credentials** (`oauth2-proxy`) — its Google OAuth client
   secret expired or was revoked in the GCP console, or its cookie secret
   changed. Both are configured on the sidecar container, not in this repo.
6. **Allowlist rejection** — the user authenticated fine but their email is
   not in `JQUANTS_ALLOWED_EMAILS`. This surfaces as a tool error, not an
   OAuth loop, and is visible in the audit log as `allowlist_rejected`.

> Two causes from the pre-1.0.0 in-process flow are **gone** and should not be
> investigated: JWT signing-key rotation invalidating sessions
> (`OAUTH_JWT_SIGNING_KEY` no longer exists) and stale `oauth_state` Firestore
> entries blocking new flows (that collection was owned by the deleted flow).

## Recovery

- **Desktop bug**: nothing to fix server-side. Advise the user to use
  mcp-stdio (`claude mcp add jquants-cloud -- uvx mcp-stdio --oauth <URL>`).
- **`PUBLIC_URL` mismatch**: correct the app container's `PUBLIC_URL`, redeploy,
  and have affected users re-add the connector.
- **Redirect URI**: add the client's callback to the gateway's
  `--allow-redirect-uri` list in `scripts/entrypoint-stdio.sh` and redeploy.
- **Sidecar credentials**: rotate the `oauth2-proxy` secret through the service
  configuration, then redeploy.
- **Allowlist**: add the email to the `jquants-allowed-emails` secret and
  redeploy (`gh workflow run cd.yml`).

## Post-incident

- If the Desktop bug is suspected, confirm via Cloud Run logs showing a
  successful token issuance for that user. Note the user-facing workaround in
  the session.
- If the cause was a URL/redirect mismatch, check whether the same value needs
  updating in the `OAUTH_BASE_URL` repo Variable (CD's smoke test) and in the
  sign-in layer's authorized redirect URI — those three drift independently.
