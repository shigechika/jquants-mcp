# Pub/Sub Auto-Reload Setup

Triggers a cache.db reload in Cloud Run whenever the self-hosted publisher
uploads a new snapshot to GCS.

## Architecture

```
Self-hosted publisher
  └─ gcs_export_cache.py
        └─ gs://${BUCKET}/jquants-mcp/cache.db (GCS object.finalize)
              └─ Pub/Sub topic: ${TOPIC}
                    └─ Push subscription → ${PUSH_URL}
                          └─ Cloud Run: download cache.db + lazy SQLite reconnect
```

## Prerequisites

```bash
PROJECT=your-gcp-project
REGION=us-west1
BUCKET=your-gcp-project-jquants-mcp  # must be globally unique
SERVICE=jquants-mcp
SA=jquants-mcp@${PROJECT}.iam.gserviceaccount.com
TOPIC=jquants-mcp-cache-updated
SERVICE_URL=https://your-service.example.com  # Cloud Run custom domain or run.app URL
PUSH_URL=${SERVICE_URL}/internal/reload
```

## Step 1 — Create Pub/Sub topic

```bash
gcloud pubsub topics create ${TOPIC} --project=${PROJECT}
```

## Step 2 — Grant GCS permission to publish to the topic

GCS sends notifications as `service-<PROJECT_NUMBER>@gs-project-accounts.iam.gserviceaccount.com`.
Find the project number first:

```bash
PROJECT_NUMBER=$(gcloud projects describe ${PROJECT} --format='value(projectNumber)')
GCS_SA=service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com

gcloud pubsub topics add-iam-policy-binding ${TOPIC} \
  --member="serviceAccount:${GCS_SA}" \
  --role=roles/pubsub.publisher \
  --project=${PROJECT}
```

## Step 3 — Create GCS bucket notification

```bash
gcloud storage buckets notifications create gs://${BUCKET} \
  --topic=${TOPIC} \
  --event-types=OBJECT_FINALIZE \
  --object-prefix=jquants-mcp/cache.db \
  --project=${PROJECT}
```

Verify:
```bash
gcloud storage buckets notifications list gs://${BUCKET} --project=${PROJECT}
```

## Step 4 — Grant Cloud Run invoker role to a Pub/Sub SA

Create a dedicated service account for Pub/Sub to call the Cloud Run endpoint:

```bash
PUBSUB_SA=pubsub-invoker@${PROJECT}.iam.gserviceaccount.com

gcloud iam service-accounts create pubsub-invoker \
  --display-name="Pub/Sub → Cloud Run invoker" \
  --project=${PROJECT}

gcloud run services add-iam-policy-binding ${SERVICE} \
  --member="serviceAccount:${PUBSUB_SA}" \
  --role=roles/run.invoker \
  --region=${REGION} \
  --project=${PROJECT}
```

## Step 5 — Create push subscription

```bash
gcloud pubsub subscriptions create jquants-mcp-cache-updated-push \
  --topic=${TOPIC} \
  --push-endpoint=${PUSH_URL} \
  --push-auth-service-account=${PUBSUB_SA} \
  --ack-deadline=30 \
  --message-retention-duration=6h \
  --project=${PROJECT}
```

## Step 6 — Configure Cloud Run environment variables

Add to the CD workflow (`cd.yml`) under `--set-env-vars`:

```
PUBSUB_INVOKER_SA=pubsub-invoker@${PROJECT}.iam.gserviceaccount.com
PUBSUB_AUDIENCE=${PUSH_URL}
```

These two variables activate OIDC verification in the `/internal/reload` endpoint.
Without them the endpoint accepts any POST request (unsafe in production).

## Step 7 — Verify

After publishing a new cache.db snapshot:

1. Check Pub/Sub subscription metrics in Cloud Console for message delivery.
2. Check Cloud Run logs for:
   ```
   Downloading gs://${BUCKET}/jquants-mcp/cache.db ...
   Downloaded cache.db from GCS (NNN.N MB)
   Cache reload scheduled (last_reload_at=...)
   ```
3. Call `health_check` and confirm `last_reload_at` is set.

## Cleanup (if needed)

```bash
gcloud pubsub subscriptions delete jquants-mcp-cache-updated-push --project=${PROJECT}
gcloud pubsub topics delete ${TOPIC} --project=${PROJECT}
gcloud storage buckets notifications delete gs://${BUCKET} --project=${PROJECT}
```
