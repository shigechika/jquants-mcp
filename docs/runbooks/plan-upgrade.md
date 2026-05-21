# J-Quants API plan upgrade / downgrade

Checklist for switching from Light to Standard or Premium (and reverting).

---

## Upgrade: Light → Standard

### Day-of steps

#### 1. Register the new API key

After completing the J-Quants plan upgrade on the J-Quants web console,
register the (new or same) API key:

```text
# Via MCP tool
register_api_key(api_key="<key>")
# → expected: {"plan": "standard", ...}
```

Or via the `/settings` web UI on Cloud Run.

Confirm plan detection:

```text
health_check()
# → {"status": "ok", "plan": "standard", ...}
```

#### 2. Update daily_fetch plan setting (Cloud Run only)

**Self-hosted** — No action needed. `daily_fetch.py` auto-detects the plan
from the API on each run.

**Cloud Run** — Edit `.github/workflows/cd.yml`, set
`JQUANTS_PLAN=standard` in the `--set-env-vars` block, then push to
trigger CD. This controls cache date-range restrictions on the MCP server.

#### 3. Populate historical Tier 1 cache (Standard endpoints)

Run daily_fetch once to seed the new tables:

```bash
uv run python scripts/daily_fetch.py

# Verify new tables received rows
sqlite3 cache.db "SELECT COUNT(*) FROM markets_margin_interest;"
sqlite3 cache.db "SELECT COUNT(*) FROM markets_margin_alert;"
sqlite3 cache.db "SELECT COUNT(*) FROM markets_short_ratio;"
# Expected: each several thousand rows after the first successful run
```

For historical depth beyond daily_fetch lookback, use bulk_fetch_all.py:

```bash
# Pull margin-interest from 2024-01-01 onward
uv run python scripts/bulk_fetch_all.py --endpoints margin_interest --from 20240101
uv run python scripts/bulk_fetch_all.py --endpoints short_ratio --from 20240101
```

> **Note:** `markets_short_sale_report` currently has no incremental
> Tier 1 cache (PR D pending). daily_fetch seeds today's data only.
> Historical short-sale-report data is not backfilled by default.

#### 4. Verify each newly unlocked tool

Run these MCP tool calls the day of upgrade (substitute today's date):

| Tool | Sample call | Expected |
|---|---|---|
| `get_indices_bars_daily` | `code="0000", date_from="2026-05-01"` | ≥ 15 rows, OHLC fields non-null (Tier 2 — API call; not populated by daily_fetch) |
| `get_markets_margin_interest` | `date="<today>", detail=True` | count > 3000 |
| `get_markets_margin_alert` | `date="<today>"` | count ≥ 0 (may be 0 on non-alert days) |
| `get_markets_short_sale_report` | `date="<today>"` | count > 0 |
| `get_markets_short_ratio` | `date="<today>"` | count > 3000 |
| `get_derivatives_bars_daily_futures` | `date="<today>"` | count > 0 |
| `get_sector_briefing` | `sector_type="s33"` | `margin_ratio_median` non-null on ≥ 1 sector |

> **Timing note:** `margin_alert` and `margin_interest` are published at
> ~16:30 JST. Querying for today's date before that time returns no data
> (not a bug).

#### 5. Cache-only tools: confirm enriched output

These tools are cache-only; they use margin_interest data once it's cached:

- `get_market_briefing()` — check `margin_ratio_*` fields
- `get_stock_briefing(code="<code>")` — `margin_interest.ratio` field should be non-null
- `get_sector_briefing()` — `margin_ratio_median` should be non-null

---

## Upgrade: Standard → Premium

After completing the J-Quants plan upgrade, repeat step 1 above to
re-register the API key. `daily_fetch.py` on self-hosted deployments will
auto-detect the new plan. For Cloud Run, update `JQUANTS_PLAN=premium` in
`cd.yml`.

### Additional Premium-only tools to verify

| Tool | Sample call | Expected |
|---|---|---|
| `get_markets_breakdown` | `date="<today>"` | count > 0 |
| `get_derivatives_bars_daily_options` | `date="<today>"` | count > 0 |
| `get_derivatives_bars_daily_options_225` | `date="<today>"` | count > 0 |
| `get_equities_bars_daily_am` | `code="27800", date="<today>"` | OHLC with AM session data |
| `get_fins_details` | `code="27800"` | detailed financial fields |

---

## Known gaps (pending PRs)

| Issue | Affected tool | Workaround |
|---|---|---|
| `get_indices_bars_daily` is Tier 2 only; first cold fetch is slow on Cloud Run | `get_indices_bars_daily` | Pre-warm by calling once after daily_fetch; subsequent calls hit Tier 2 cache |
| `date_field="PubDate"` may not match stored JSON field (`AppDate`) | `get_markets_margin_alert` | Use `date_from` / `date_to` filters (code+date path always works) |
| `get_markets_short_sale_report` has no incremental Tier 1 | `get_markets_short_sale_report` | Query by recent date range; historical data requires bulk re-fetch |

---

## Downgrade: Standard/Premium → Light

1. **Self-hosted** — No config.ini change needed; `daily_fetch.py` auto-detects
   the new plan. **Cloud Run** — update `JQUANTS_PLAN=light` in `cd.yml` and
   push to trigger CD.
2. Restart the MCP server (or trigger CD).
3. Confirm `health_check()` returns `"plan": "light"`.
4. Standard/Premium tables remain in cache.db but are no longer refreshed;
   they will grow stale over time. To reclaim disk space (optional):

   ```bash
   sqlite3 cache.db "DELETE FROM markets_margin_interest;"
   sqlite3 cache.db "DELETE FROM markets_margin_alert;"
   sqlite3 cache.db "DELETE FROM markets_short_ratio;"
   sqlite3 cache.db "DELETE FROM markets_short_sale_report;"
   sqlite3 cache.db "VACUUM;"
   ```

5. Verify Light tools still work:

   ```text
   get_indices_bars_daily_topix(date_from="2026-05-01")
   get_markets_margin_interest()   # → PlanRestrictionError (expected)
   get_sector_briefing()           # → margin_ratio_median: null (expected)
   ```
