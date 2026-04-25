"""Shared MCP tool annotation presets.

Per the MCP specification, tools advertise a few hints (``readOnlyHint``,
``destructiveHint``, ``idempotentHint``, ``openWorldHint``) so MCP clients
can apply appropriate trust policies — for example Claude Desktop /
mobile uses these to decide whether a tool call requires a confirmation
prompt.

Centralising the annotation dicts here keeps the tool decorators across
``tools/*.py`` and ``server.py`` consistent and lets us change a policy
in one place.
"""

from __future__ import annotations

# Tools that READ data from the J-Quants API v2 with a cache layer in
# front. They never modify the API state, calling them twice produces
# the same result for the same parameters (within cache TTLs), and they
# may issue an outbound HTTP request when the cache misses.
READ_ONLY_API: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

# Tools that READ purely from the local SQLite cache without calling
# any external system. Includes the offline screener and chart tools.
READ_ONLY_CACHE: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

# Tools that READ purely server-local state (config, cache stats, health).
# Trust profile is identical to READ_ONLY_CACHE (no external system hit,
# no mutation), so we expose this as an alias rather than duplicating the
# dict. The two names live separately so call sites read intentionally —
# ``READ_ONLY_CACHE`` for cache reads, ``READ_ONLY_LOCAL`` for server-state
# reads — even though the MCP wire payload is identical.
READ_ONLY_LOCAL: dict[str, bool] = READ_ONLY_CACHE

# Tools that MUTATE server-local state (clear cache, register/delete
# per-user API key). MCP clients should require confirmation per call.
# Most of these are idempotent in practice (clearing twice = clear once,
# re-registering the same key = same final state), so ``idempotentHint``
# is True.
DESTRUCTIVE_LOCAL: dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}
