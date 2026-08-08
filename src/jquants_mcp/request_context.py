"""Request-scoped plan contextvar, read as a fallback by the cache layer.

Holds the authenticated user's subscription plan for the duration of a single
tool call so the cache layer can apply per-user plan date restrictions without
threading a ``plan`` argument through every tool. Nothing in the stdio server
sets this contextvar directly anymore (there is no per-call middleware hook in
the official mcp SDK) — ``CacheStore._effective_plan()`` reads it only as a
fallback before consulting its constructor-injected ``plan_resolver`` (see
``server.py``'s ``_resolve_current_plan`` / ``_current_user_id``). Callers that
want to pin a plan for a block of code (e.g. tests) can still push one
explicitly via ``set_current_plan``/``reset_current_plan``.

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
