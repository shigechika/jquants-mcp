# Secrets rotation

Procedure for rotating this package's one load-bearing secret.

## `MCP_ENCRYPTION_KEY` (AES-256-GCM passphrase for stored API keys)

### Planned rotation (zero-downtime)

1. **Generate the new key** and store it as a new version in Secret Manager:
   ```sh
   NEW=$(openssl rand -base64 48)
   printf "%s" "$NEW" | gcloud secrets versions add mcp-encryption-key \
     --data-file=- --project=${PROJECT}
   ```
2. **Capture the previous value** for the dual-key window:
   ```sh
   OLD=$(gcloud secrets versions access latest --secret=mcp-encryption-key --project=${PROJECT})
   # Then add the previous value as a separate secret, or pass it directly to CD
   printf "%s" "<previous value>" | gcloud secrets versions add mcp-encryption-key-previous \
     --data-file=- --project=${PROJECT}
   ```
3. **Deploy Cloud Run with both keys** — update `.github/workflows/cd.yml` to
   pass `MCP_ENCRYPTION_KEY_PREVIOUS=mcp-encryption-key-previous:latest` in
   `--set-secrets`, then merge and wait for CD. The server now decrypts
   with either key (primary first, previous on fallback) and re-encrypts
   fresh writes with the primary.
4. **Run the rotation script** to re-encrypt all existing blobs:
   ```sh
   OLD=$(gcloud secrets versions access 1 --secret=mcp-encryption-key-previous --project=${PROJECT})
   NEW=$(gcloud secrets versions access latest --secret=mcp-encryption-key --project=${PROJECT})
   uv run python scripts/rotate_encryption_key.py \
     --project=${PROJECT} \
     --old-key "$OLD" --new-key "$NEW" --dry-run
   # Review dry-run output, then run for real
   uv run python scripts/rotate_encryption_key.py \
     --project=${PROJECT} \
     --old-key "$OLD" --new-key "$NEW"
   ```
5. **Remove the previous key** once the script reports all success: edit
   `.github/workflows/cd.yml` to drop the `MCP_ENCRYPTION_KEY_PREVIOUS`
   entry, delete the `mcp-encryption-key-previous` secret, merge.

### Emergency rotation (suspected leak)

Same flow but skip the dual-key window: deploy with the new key only,
accept that all stored API keys become unreadable, notify users to
re-register. Users re-run the `register_api_key` MCP tool — asking Claude
to call it in a normal chat is the whole procedure; there is no web page
for it.

## Session-signing keys

**Not this package's secret any more.** The in-process OAuth flow — and
with it `OAUTH_JWT_SIGNING_KEY` — was removed in 1.0.0. Sessions are
issued and signed by the layers in front of the stdio server:

- **`mcp-stdio serve`** issues the MCP OAuth tokens on `/mcp` and persists
  them in Firestore (`--token-store-firestore`, see
  `scripts/entrypoint-stdio.sh`). Because they are persisted, a redeploy
  or instance recycle no longer invalidates issued tokens.
- **`oauth2-proxy`** (Cloud Run ingress sidecar) holds the user sign-in
  cookie secret and the Google OAuth client secret.

Rotate either one through that layer's own configuration on the Cloud Run
service. Neither secret is referenced by this repo's code or by `cd.yml`.

## Post-rotation checklist

- [ ] Cloud Run serves normally (check `health_check` and one tool call)
- [ ] Audit log shows no decrypt failures (`action=rate_limited` aside)
- [ ] Old secret version disabled (not deleted — keep for forensics):
      ```sh
      gcloud secrets versions disable <OLD_VERSION> --secret=<SECRET> --project=${PROJECT}
      ```
- [ ] Incident notes saved to memory if the rotation was leak-driven
