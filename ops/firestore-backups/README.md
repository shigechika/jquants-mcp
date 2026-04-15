# Firestore backup schedules

Managed backup schedules for the `(default)` Firestore database on
`aikawa-dx`. Set up once; Firestore runs them server-side.

## Active schedules

| Recurrence | Retention | Created |
|---|---|---|
| Daily | 7 days | 2026-04-15 |
| Weekly (Sunday) | 14 weeks | 2026-04-15 |

## Create (idempotent reference)

```sh
# Daily, 7-day retention
gcloud firestore backups schedules create \
  --project=aikawa-dx \
  --database='(default)' \
  --recurrence=daily \
  --retention=7d

# Weekly (Sunday), 14-week retention
gcloud firestore backups schedules create \
  --project=aikawa-dx \
  --database='(default)' \
  --recurrence=weekly \
  --retention=14w \
  --day-of-week=sun
```

## Inspect

```sh
gcloud firestore backups schedules list \
  --project=aikawa-dx --database='(default)'

gcloud firestore backups list --project=aikawa-dx
```

## Restore

See [`docs/runbooks/firestore-restore.md`](../../docs/runbooks/firestore-restore.md).
