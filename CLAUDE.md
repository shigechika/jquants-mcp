# CLAUDE.md

## Project Overview

jquants-dat-mcp is an MCP server that retrieves Japanese stock market data via J-Quants API v2.
Built with FastMCP v3, httpx, pydantic-settings, SQLite cache.

## Commands

```bash
uv sync --dev          # Install dependencies
uv run pytest -v       # Run tests
uv run ruff check src/ tests/   # Lint
uv run ruff format src/ tests/  # Format
```

## Architecture

- `src/jquants_dat_mcp/` — Main package
  - `server.py` — FastMCP server, lazy globals (_settings, _client, _cache)
  - `client.py` — httpx async client with rate limiting, retry, pagination
  - `config.py` — configparser + env vars hybrid configuration
  - `cache/store.py` — 2-tier SQLite cache (Tier1: row-level, Tier2: response-level with TTL)
  - `tools/` — Tool modules registered via `register(mcp, get_client, get_cache)` pattern
- `tests/` — pytest + pytest-asyncio tests

## Key Patterns

- Tools are closures inside `register()` functions, capturing `get_client` and `get_cache` callables
- Tests patch `server_module._settings`, `_client`, `_cache` globals directly
- `_call()` helper uses `mcp.call_tool(name, kwargs)` then parses `result.content[0].text`
- docstring は英語で記述、コード内コメントは日本語
- README.md は英語、README.ja.md は日本語
