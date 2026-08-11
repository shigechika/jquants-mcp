# Pub/Sub auto-reload — retired

**This design is gone. Nothing here needs to be provisioned.**

The push-reload path (GCS `OBJECT_FINALIZE` → Pub/Sub topic → push
subscription → Cloud Run `/internal/reload`) was how a freshly published
`cache.db` used to reach a running Cloud Run instance. It no longer exists:

- Push-based cache refresh was evaluated and **rejected** (#584): the
  CD-deployed `jquants` service runs with `min-instances=0`, so every cold
  start already re-downloads `cache.db` (`scripts/entrypoint-stdio.sh`
  Step 2), and measured instance lifetimes are 15–26 min — the window a
  refresh mechanism could help is bounded by roughly that.
- A stdio-only server behind `mcp-stdio serve` exposes no HTTP route for a
  push to land on, and the `/internal/reload` endpoint (with its
  `PUBSUB_INVOKER_SA` / `PUBSUB_AUDIENCE` OIDC verification) was deleted
  along with the rest of the HTTP surface.

The operational consequence, in one line: **publishing to the topic is still
accepted and still reports success, but reloads nothing.** Do not reach for
it during an incident — see
[`docs/runbooks/cache-db-missing.md`](../../docs/runbooks/cache-db-missing.md)
for what actually works (wait for the recycle, or deploy a new revision).

## Teardown of the leftover GCP resources

The topic and the GCS bucket notification were never deleted, which is why a
publish still succeeds. Remove them in this order (subscription first, so no
delivery is attempted against a half-removed chain):

```bash
PROJECT=your-gcp-project
BUCKET=your-gcp-project-jquants-mcp
TOPIC=jquants-mcp-cache-updated

# 1. Push subscription (may already be absent)
gcloud pubsub subscriptions delete jquants-mcp-cache-updated-push --project=${PROJECT}

# 2. GCS bucket notification (find the notification id first)
gcloud storage buckets notifications list gs://${BUCKET} --project=${PROJECT}
gcloud storage buckets notifications delete gs://${BUCKET} --project=${PROJECT}

# 3. Topic
gcloud pubsub topics delete ${TOPIC} --project=${PROJECT}
```

Best-effort, if nothing else uses them: the dedicated invoker service account
created for this path, and its `run.invoker` binding. The binding was granted
on the old `jquants-mcp` service, so it is likely already gone with that
service (#568) — check before deleting the SA.

```bash
PUBSUB_SA=pubsub-invoker@${PROJECT}.iam.gserviceaccount.com

gcloud projects get-iam-policy ${PROJECT} \
  --flatten="bindings[].members" --filter="bindings.members:${PUBSUB_SA}" \
  --format="table(bindings.role)"
gcloud iam service-accounts delete ${PUBSUB_SA} --project=${PROJECT}
```

Also drop `PUBSUB_INVOKER_SA` / `PUBSUB_AUDIENCE` from the Cloud Run service's
env vars if any revision still carries them; the code that read them is gone.
