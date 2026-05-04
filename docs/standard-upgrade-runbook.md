# Standard Plan Upgrade Runbook

Run this procedure after upgrading your J-Quants subscription from Light to Standard
(or higher) to backfill all historical data that the Bulk API now makes available.

## What changes between Light and Standard

| Endpoint | Light | Standard |
|---|---|---|
| `/equities/bars/daily` | 12 months rolling | full history (2016-03+) |
| `/markets/short-ratio` | ✗ | ✓ |
| `/markets/margin-interest` | ✗ | ✓ |
| `/markets/margin-alert` | ✗ | ✓ |
| `/markets/short-sale-report` | ✗ | ✓ |
| `/indices/bars/daily` | ✗ | ✓ |
| `/derivatives/bars/daily/options/225` | ✗ | ✓ |

> **Note:** `/markets/breakdown` requires Premium. It is skipped automatically on Standard.

## Prerequisites

- Standard (or higher) plan is active on your J-Quants account
- API key is configured (`~/.config/jquants-mcp/config.toml` or `JQUANTS_API_KEY` env var)
- Python environment is set up: `cd ~/src/kb/jquants-mcp && uv sync`

## Step 1 — Update plan in config

Set `jquants_plan = "standard"` so that `daily_fetch.py` starts picking up
Standard-only endpoints in future incremental runs:

```toml
# ~/.config/jquants-mcp/config.toml
jquants_plan = "standard"
```

## Step 2 — Hydrate cache.db with full history

```bash
cd ~/src/kb/jquants-mcp
uv run python scripts/bulk_fetch_all.py
```

This fetches every endpoint in `ENDPOINTS` (defined in `scripts/bulk_fetch_all.py`).
Any endpoint that returns 403 due to a plan restriction is logged as a warning and
skipped automatically — it is safe to run without `--endpoints` to fetch everything.

Estimated time: 30–90 minutes depending on network speed and plan rate limits.

To preview what files would be downloaded without writing anything:

```bash
uv run python scripts/bulk_fetch_all.py --dry-run
```

To fetch only a subset of endpoints:

```bash
uv run python scripts/bulk_fetch_all.py --endpoints short_ratio margin_interest margin_alert
```

## Verification

Check row counts and date coverage after the backfill:

```bash
# Quick sanity check
sqlite3 ~/.cache/jquants-mcp/cache.db "
SELECT 'equities_bars_daily',  MIN(date), MAX(date), COUNT(*) FROM equities_bars_daily
UNION ALL
SELECT 'markets_short_ratio',  MIN(date), MAX(date), COUNT(*) FROM markets_short_ratio
UNION ALL
SELECT 'markets_margin_interest', MIN(date), MAX(date), COUNT(*) FROM markets_margin_interest
UNION ALL
SELECT 'indices_bars_daily',   MIN(date), MAX(date), COUNT(*) FROM indices_bars_daily;
"

# Full gap check
cd ~/src/kb/jquants-mcp
uv run python scripts/verify_cache_completeness.py
```

## Notes

- `daily_fetch.py` reads `ENDPOINT_MIN_PLAN` at runtime, so incremental updates for
  Standard-only endpoints start automatically once Step 1 is done.
- Standard-only tables keep their rows in `cache.db` if you later downgrade to Light,
  but they will no longer receive incremental updates until you upgrade again.
- If your workflow manages an external stock price CSV (e.g. populated via
  `get_eq_bars_daily_range()`), rebuild it from scratch after upgrading — Light plan
  limits that call to the most recent 12 months, leaving older data missing.
