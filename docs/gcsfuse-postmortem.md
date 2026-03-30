# gcsfuse + SQLite: Post-mortem

Date: 2026-03-30

## Summary

Attempted to use gcsfuse (Google Cloud Storage FUSE) to mount a 9.2 GB
read-only SQLite database on Cloud Run. **Failed.** Reverted to startup-copy
approach.

## What we tried

| Attempt | Result |
|---------|--------|
| gcsfuse read-only mount (no cache) | `unable to open database file` — SQLite tries to create journal files |
| `immutable=1` URI parameter | Connection succeeds, but queries timeout (504) |
| gcsfuse + `file-cache-max-size-mb=10240` | Same 504 timeout — file cache doesn't help for initial queries |

## Root cause

gcsfuse serves data via GCS HTTP API. Each SQLite page read (4 KB) is a
network round-trip. A single `SELECT COUNT(*)` on a 9.2 GB database
traverses millions of B-tree pages, causing timeouts.

gcsfuse file cache downloads pages on first access, but:
- SQLite's random access pattern means many individual HTTP requests
- The initial cache warming takes too long for Cloud Run's request timeout
- `cache-file-for-range-read` caches the *entire file* (9.2 GB), not individual pages

## What works

SQLite requires **local disk** with microsecond I/O latency. On Cloud Run:
- Copy cache.db from GCS to `/tmp` (tmpfs) at startup
- Use startup probe to delay traffic until copy is complete
- `immutable=1` is still useful to avoid journal file creation

## Key learnings

1. **gcsfuse is not suitable for SQLite databases > ~100 MB** due to random read latency
2. **`immutable=1`** is required for SQLite on any read-only filesystem (prevents -shm/-wal creation)
3. **SQLite's .shm/.wal files cannot be relocated** to a different directory — no PRAGMA or option exists
4. **`PRAGMA mmap_size`** should NOT be used on network filesystems (SIGBUS on I/O error)
5. **Always research technology compatibility BEFORE implementing** — "gcsfuse sqlite" search would have found these issues immediately

## References

- [gcsfuse Issue #38: Small random reads](https://github.com/GoogleCloudPlatform/gcsfuse/issues/38)
- [SQLite URI Parameters](https://sqlite.org/uri.html) — `immutable=1`
- [SQLite WAL Documentation](https://www.sqlite.org/wal.html)
- [Google Cloud: gcsfuse file cache](https://docs.cloud.google.com/storage/docs/gcsfuse-file-cache)
- [Google Cloud: Cloud Run volume mounts](https://docs.cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts)
