"""Structured audit logging for user actions."""

from __future__ import annotations

import json
import logging
import time

_audit_logger = logging.getLogger("jquants_dat_mcp.audit")


def audit(action: str, user_id: str | None = None, **fields) -> None:
    """Write a structured JSON audit log entry.

    Args:
        action: The action being audited (e.g. "register_api_key", "tool_call").
        user_id: Authenticated user identifier. Omitted for unauthenticated actions.
        **fields: Additional context fields to include in the log entry.
    """
    entry: dict = {"ts": time.time(), "action": action}
    if user_id is not None:
        entry["user_id"] = user_id
    entry.update(fields)
    _audit_logger.info(json.dumps(entry, ensure_ascii=False))
