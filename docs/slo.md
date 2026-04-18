# Service Level Objectives

Minimal SLO set for the Cloud Run `jquants-mcp` deployment. Three
numbers, 30-day rolling window. The goal is to have *a* principled
answer to "is the service healthy?", not to chase nines.

## Status

**Provisional — targets proposed from #72/#73 load-test baselines, to
be calibrated after 1–2 months of real-traffic data.** The load test
showed p95 ≈ 180 ms / p99 ≈ 350 ms on cache hits; the targets below
leave generous headroom.

## Objectives

| SLO | Target | Window | Rationale |
|---|---|---|---|
| Availability (non-5xx ratio) | **99.5%** | 30 days rolling | Allows ~3.6 h of downtime/month. Honest given single-region deployment + no automated failover (see [dr.md](dr.md)) |
| Tool-call latency p95 (cache hit) | **< 1000 ms** | 30 days rolling | Load-test baseline p95 is ~180 ms; 1 s absorbs the long tail of J-Quants fallback hits that still complete |
| Tool-call latency p99 (cache hit) | **< 2500 ms** | 30 days rolling | Same rationale, more slack for the heavy-response tail |

**Excluded from SLO scope:**

- **API fallback latency** — depends on J-Quants upstream, not under our control. We measure it but do not target it.
- **Cold-start cache.db download window** — ~2 min of degraded (API-only) service after a revision deploy or scale-from-zero. No requests fail, they just skip the Tier 1 cache. Accepted.
- **OAuth flow success rate** — dominated by known Claude Desktop bug #40102 (client-side). A server-side `/token` success SLO could be added if we want to monitor *our* contribution.

## Measurement

All via Cloud Monitoring on resource `cloud_run_revision` filtered to
`service_name=jquants-mcp`.

### Availability

```
fetch cloud_run_revision
| metric 'run.googleapis.com/request_count'
| filter resource.service_name == 'jquants-dat-mcp'
| align rate(1m)
| {
    total: group_by [], sum(val());
    errs:  filter metric.response_code_class == '5xx'
           | group_by [], sum(val())
  }
| join
| value [sli: 1 - val(1) / val(0)]
| every 1m
```

The existing `02-5xx-rate.yaml` alert (> 1% 5xx over 10 min) fires
well before the 99.5% / 30-day budget is fully burned.

### Latency percentiles

```
fetch cloud_run_revision
| metric 'run.googleapis.com/request_latencies'
| filter resource.service_name == 'jquants-dat-mcp'
| align percentile(95, 1m)   # or percentile(99, 1m)
| group_by [], max(val())
| every 1m
```

This includes API-fallback requests; the load test showed that dominates
the p99 tail. If the cache-hit-only slice is needed, we will need to add
a custom metric that tags tool-call latency with a `cache_hit` label —
deferred until the aggregate p99 proves insufficient.

## Error budget policy

**30-day budget** at 99.5% availability = 0.5% × (30 days × 24 h) ≈ 3.6 h
of downtime.

| Budget remaining | Action |
|---|---|
| > 50% | Normal — ship at will, including risky changes |
| 20–50% | Caution — prefer smaller PRs, avoid deploying on weekends |
| < 20% | Freeze non-critical changes. Prioritize reliability work until budget recovers |
| Exhausted / negative | Declare incident, root-cause the burn. No feature deploys until budget positive and the cause is understood |

Latency SLO violations follow the same tiering but are less strict —
sustained breach of p95 > 1 s triggers investigation, not a freeze.

## Cloud Monitoring native SLOs (follow-up)

Cloud Monitoring supports native SLO resources via `gcloud beta monitoring services` + `slos create`, which gives you budget-burn dashboards and alerts in the console UI. Deferred until we have ~4 weeks of traffic to calibrate the targets against reality. See this page as the source of truth until then.

## Post-calibration review

- [ ] After 1 month of production traffic, compare actual p95 / p99 / availability against these targets
- [ ] Tighten or loosen each number with a one-sentence justification
- [ ] Decide whether to create Cloud Monitoring SLO resources at that point
