"""Request-scoped context shared between the server middleware and the cache.

Holds the authenticated user's subscription plan for the duration of a single
tool call so the cache layer can apply per-user plan date restrictions without
threading a ``plan`` argument through every tool. The value is set by the MCP
``on_call_tool`` middleware (see ``server.PlanContextMiddleware``) and read by
``CacheStore`` when no explicit plan is passed.

Uses ``contextvars`` so the value is isolated per async task / request. Default
is ``None``, which means "no per-user plan" — the cache falls back to its own
configured ``default_plan`` (single-user / bearer / unauthenticated paths).
"""

from __future__ import annotations

import contextvars

_current_plan: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jquants_current_plan", default=None
)


def set_current_plan(plan: str | None) -> contextvars.Token:
    """Set the current request's plan; returns a token for ``reset_current_plan``."""
    return _current_plan.set(plan)


def reset_current_plan(token: contextvars.Token) -> None:
    """Restore the previous plan value. Must run in a ``finally`` to avoid bleed."""
    _current_plan.reset(token)


def get_current_plan() -> str | None:
    """Return the current request's plan, or ``None`` when unset."""
    return _current_plan.get()
