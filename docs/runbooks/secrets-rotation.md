# Secrets rotation

Procedures for rotating the two load-bearing secrets.

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
re-register. Users re-run `register_api_key` through the MCP tool or
the `/settings` page.

## `OAUTH_JWT_SIGNING_KEY` (JWT signer for OAuth sessions)

Cloud Run is stateless per request, so rotation is simpler.

### Planned rotation

Accept that all existing sessions are invalidated on deploy. Users will
see one "please log in again" flow and recover. For this user base the
operational cost is acceptable.

1. **Generate a new key**:
   ```sh
   openssl rand -base64 48 \
     | gcloud secrets versions add OAUTH_JWT_SIGNING_KEY \
         --data-file=- --project=${PROJECT}
   ```
2. **Redeploy via CD** — `workflow_dispatch` on the CD workflow, or push
   any main commit. The new revision picks up `OAUTH_JWT_SIGNING_KEY:latest`.
3. **Notify affected users** if the user count warrants it; otherwise
   rely on the "please log in again" UX.

### Emergency rotation (suspected leak)

Identical to planned: generate, push, redeploy. The key loss window is
capped at the redeploy time (a few minutes).

### Parallel validation (dual-`kid` JWT) — deferred

True zero-downtime JWT rotation would require `kid`-based multi-key
validation in the upstream FastMCP `GoogleProvider`. That is a larger
surgery than the current session-invalidation cost justifies. Revisit
if the user base grows past the point where "re-login" is a noticeable
outage.

## Post-rotation checklist

- [ ] Cloud Run serves normally (check `health_check` and one tool call)
- [ ] Audit log shows no decrypt failures (`action=rate_limited` aside)
- [ ] Old secret version disabled (not deleted — keep for forensics):
      ```sh
      gcloud secrets versions disable <OLD_VERSION> --secret=<SECRET> --project=${PROJECT}
      ```
- [ ] Incident notes saved to memory if the rotation was leak-driven
