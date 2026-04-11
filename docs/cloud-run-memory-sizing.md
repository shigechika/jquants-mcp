# Cloud Run memory sizing (8 GiB → 6 GiB)

Date: 2026-04-11
Issue: [#72](https://github.com/shigechika/jquants-dat-mcp/issues/72)
PR: [#73](https://github.com/shigechika/jquants-dat-mcp/pull/73)

## Summary

After `cache.db` shrank from 9.2 GB → 5.7 GB → 3.57 GB through field
normalization and migration, the 8 GiB Cloud Run memory allocation became a
candidate for reduction. We ran a 6-phase load test against the live
deployment, measured peak memory under load, and reduced memory from 8 GiB
to 6 GiB. vCPU stayed at 2. No regression observed.

**Outcome:** memory p99 stayed at ~3850 MiB in both 8 GiB and 6 GiB
configurations (47% → 61% of the new limit), latency and error rate were
unchanged, and there is still ~2.2 GiB of headroom on the 6 GiB instance.

## Motivation

- `cache.db` is materialized into `/tmp` (tmpfs = RAM) at startup, so the
  memory limit has to cover `cache.db` + Python runtime + request-time
  allocations.
- Three recent changes combined to make 8 GiB look over-provisioned:
  1. `cache.db` shrank to 3.57 GB (field normalization + VACUUM).
  2. `users.db` and `oauth_state.db` were moved to Firestore, removing the
     "multi-instance SQLite contention" concern.
  3. `gcs_export_cache.py` now sets `PRAGMA user_version=1`, eliminating the
     97-second migration scan on cold start.
- Issue [#72](https://github.com/shigechika/jquants-dat-mcp/issues/72)
  required evidence-based sizing, not a guess.

## Methodology

### Scripts (kept in-tree for future re-runs)

- [`scripts/load_test.py`](../scripts/load_test.py) — 6-phase async
  workload generator. Writes one JSONL line per request with timestamps,
  latency, and status.
- [`scripts/collect_metrics.py`](../scripts/collect_metrics.py) — queries
  the Cloud Monitoring v3 API for `container/memory/utilizations` and
  `container/cpu/utilizations` over the JSONL window, aligns to 60 s
  percentile buckets, and prints per-phase + overall p95/p99 with an
  absolute MiB / vCPU verdict.

### Phase design

Originally issue #72 called for "15 years daily bars × 5 issues". The Light
plan only retains 5 years, so we substituted **5 years × 15 large-cap
issues** (Toyota, Sony, SoftBank Group, MUFG, NTT, Keyence, Tokyo Electron,
Fast Retailing, Mitsubishi Corp, SMFG, Recruit, Shin-Etsu Chem, Hitachi,
Nintendo, Tokio Marine) for an equivalent row count and JSON serialization
pressure.

| Phase | Workload | Target |
|---|---|---|
| 1. warmup | `cache_status` × 1 | touch the cold instance |
| 2. steady | light tools × 30 at 2 s intervals | baseline load |
| 3. heavy_mem | `get_equities_bars_daily(code)` × 15 issues sequentially | JSON serialization pressure |
| 4. parallel | heavy query × 3 concurrent for 120 s | sustained multi-worker load |
| 5. burst | heavy 60% + light 40% × 10 concurrent for 30 s | peak concurrency |
| 6. cooldown | idle for 60 s | recovery observation |

A 15 s gap separates each phase so that the 60 s alignment windows of Cloud
Monitoring do not span phase boundaries. Each parallel phase uses a fresh
`MCPSession` per worker to avoid `Mcp-Session-Id` interleaving.

### Cache clear strategy

We wanted to stress the "DB → JSON serialization on every request" path,
not the response-cache fast path. Two safe options considered:

1. **Clear only `response_cache` (Tier 2)** — Tier 1 row-level rows in
   `cache.db` on tmpfs remain. All requests run the SELECT → JSON pipeline.
   Safe, deterministic.
2. **Randomize issue codes to miss Tier 1** — causes real J-Quants API
   calls, hits rate limits, introduces network I/O as a confound. Rejected.

**Unsafe option:** calling `cache_clear()` with no argument deletes **all
Tier 1 tables plus `response_cache`** (see `cache/store.py:795`). On Cloud
Run this wipes the tmpfs copy of `cache.db`, which is not recovered until
the next cold start, because the GCS copy is owned by the self-hosted
server. We implemented `load_test.py --clear-response-cache` so that only
Tier 2 is cleared, and added guard documentation.

### Execution

```bash
# Against the 8 GiB deployment (pre-change baseline)
uv run scripts/load_test.py --clear-response-cache \
  --output load_test_results/run_nocache_20260411_105743.jsonl

# Deploy 6 GiB via PR #73, wait for cold start + cache.db download
# Then re-run the same test
uv run scripts/load_test.py --clear-response-cache \
  --output load_test_results/run_6gi_20260411_112244.jsonl

# Collect metrics for each run (note --memory-gib for the 6 GiB case)
uv run scripts/collect_metrics.py \
  --jsonl load_test_results/run_nocache_20260411_105743.jsonl
uv run scripts/collect_metrics.py --memory-gib 6.0 \
  --jsonl load_test_results/run_6gi_20260411_112244.jsonl
```

Each run generates 900+ requests over ~380 seconds. JSONL files live in
`load_test_results/` which is gitignored.

## Results

### Latency

| Phase | n | p50 (8 GiB) | p95 (8 GiB) | p50 (6 GiB) | p95 (6 GiB) |
|---|---|---|---|---|---|
| warmup    |   1 |  456 ms |  456 ms |  417 ms |  417 ms |
| steady    |  30 |  270 ms |  919 ms |  290 ms |  891 ms |
| heavy_mem |  15 | 1321 ms | 1701 ms | 1342 ms | 1649 ms |
| parallel  | ~650 |  509 ms |  649 ms |  515 ms |  628 ms |
| burst     | ~215 | 1366 ms | 1787 ms | 1307 ms | 1761 ms |

Errors: 0/933 on 8 GiB, 0/920 on 6 GiB.

### Resource utilization (overall p99, 60 s alignment)

| Metric | 8 GiB config | 6 GiB config |
|---|---|---|
| Memory (absolute) | 3849 MiB | 3747 MiB |
| Memory (% of limit) | 47.0% | 61.0% |
| CPU (absolute) | 0.92 vCPU | 0.90 vCPU |
| CPU (% of 2 vCPU) | 46.0% | 45.0% |

Memory absolute value actually decreased slightly after the reduction, which
is within noise range for the 60 s alignment + percentile aggregation.
Importantly, it did not increase.

### Baseline decomposition

```
3850 MiB observed
≈ 3570 MiB  cache.db on tmpfs
+  280 MiB  Python runtime + fastmcp + sqlite + httpx
+   ~0 MiB  request-time allocations (transient, smoothed by 60 s window)
```

Cloud Monitoring's 60 s alignment window smooths sub-second peaks from JSON
serialization. The "true" peak during heavy serialization is unknown from
these metrics alone, but the fact that overall p99 stays flat across phases
implies it is small relative to the 60 s bucket average.

## Sizing verdict

| Target | 1.5x safety margin (5.6 GiB needed) | Verdict |
|---|---|---|
| 4 GiB | over budget — baseline alone is 3.76 GiB | **FAIL** |
| **6 GiB** | under budget by ~400 MiB | **OK — adopted** |
| 8 GiB (original) | under budget by ~4.2 GiB | overprovisioned |

**vCPU stays at 2.** p99 = 0.92 vCPU with the 60 s window smoothing real
peaks. Dropping to 1 vCPU was not tested and would leave no safety margin.

## Gotchas

### 1. `cache_clear()` with no argument is destructive

`cache/store.py:795` iterates over `list(_TIER1_TABLES.keys()) + ["response_cache"]`
when `table=None`. On Cloud Run, the tmpfs `cache.db` is not re-downloaded
until the next cold start, so a bare `cache_clear()` call is effectively a
service-wide data wipe until restart. Always pass
`table="response_cache"` when you mean "just clear Tier 2".

### 2. Clearing `response_cache` does not change peak memory

Tier 2 (response_cache) misses fall through to Tier 1 (SQLite row-level
cache) which is already on tmpfs. The DB → JSON path runs at SQLite speed,
and peak memory is dominated by the resident `cache.db` tmpfs allocation,
not by transient per-request objects. Both "cache warm" and
"response_cache cleared" runs produced identical p99 numbers (~3849 MiB).

**Implication:** to observe a true worst-case memory peak, you would need
to cold-start the instance with an empty `cache.db` — which is not a state
the production deployment ever reaches.

### 3. Per-phase metrics break down for short phases

`collect_metrics.py` aligns samples to 60 s windows. Phases shorter than
60 s (`heavy_mem` = 21 s, `burst` = 30 s, `cooldown` = 60 s) often produce
"0 MiB" rows because the alignment window end-times fall outside the phase
window. The OVERALL row is correct and is what the sizing verdict uses.

Future improvement: extend `points_in_window` to tolerate ±60 s on phase
boundaries, or fetch raw non-aligned distribution data and aggregate
client-side. Not blocking for the sizing decision.

### 4. Cloud Run tmpfs size ≈ instance memory

There is no separate tmpfs quota on Cloud Run gen2 — `/tmp` is backed by
instance memory, and writes count against the memory limit. This means:

```
minimum memory limit = cache.db size + runtime overhead + headroom
                     ≈ cache.db size + ~300 MiB + ~1.5 GiB safety
```

For the current 3.57 GiB `cache.db`, that lands at ~5.4 GiB minimum. 6 GiB
gives a comfortable margin; 4 GiB would be unsafe.

### 5. `cache.db` is downloaded asynchronously at cold start

The entrypoint starts the MCP server immediately and runs `gcs_sync.py
--init-cache` in the background, signaling `SIGHUP` on completion. During
the 1–2 minute download window:

- Requests hit the live J-Quants API instead of Tier 1 cache.
- `cache_status` returns only `db_path` and `plan` (no row counts, no
  `db_size_mb`). This briefly confused the initial smoke test — the minimal
  payload was not a 6 GiB regression, it was the normal cold-start window.

The post-deploy smoke test for this issue would have been more convincing
if `cache_status` returned an explicit "cache not yet loaded" indicator.
Filed as a future polish.

## Future considerations

- **When to revisit:** if `cache.db` shrinks below ~2.5 GiB (re-examine 4
  GiB option) or grows above ~4 GiB (bump to 8 GiB), or if real users
  consistently exercise a workload heavier than the 10-concurrent burst.
- **vCPU downsizing:** not tested. The current p99 of 0.92 vCPU with 60 s
  smoothing leaves insufficient signal to justify dropping to 1 vCPU.
  Would require a separate test with finer-grained metrics (e.g. raw
  distribution points) before committing.
- **Burst concurrency ceiling:** Cloud Run `containerConcurrency` defaults
  to 80 per instance (current deployment uses `--concurrency=320`
  implicitly via default). The 10-concurrent burst did not stress this
  limit; there is significant unused headroom for real traffic spikes.

## References

- [`scripts/load_test.py`](../scripts/load_test.py)
- [`scripts/collect_metrics.py`](../scripts/collect_metrics.py)
- [`.github/workflows/cd.yml`](../.github/workflows/cd.yml) — single source
  of truth for the `--memory 6Gi --cpu 2` setting
- [`docs/gcsfuse-postmortem.md`](gcsfuse-postmortem.md) — the previous
  sizing-related incident that motivated the startup-copy architecture
- Issue [#72](https://github.com/shigechika/jquants-dat-mcp/issues/72),
  PR [#73](https://github.com/shigechika/jquants-dat-mcp/pull/73)
