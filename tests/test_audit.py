"""Tests for structured audit logging."""

import json
import logging


def test_audit_emits_json(caplog):
    """audit() emits a valid JSON log entry to the audit logger."""
    from jquants_dat_mcp.audit import audit

    with caplog.at_level(logging.INFO, logger="jquants_dat_mcp.audit"):
        audit("register_api_key", user_id="gh-12345", plan="light")

    assert len(caplog.records) == 1
    entry = json.loads(caplog.records[0].message)
    assert entry["action"] == "register_api_key"
    assert entry["user_id"] == "gh-12345"
    assert entry["plan"] == "light"
    assert "ts" in entry


def test_audit_without_user_id(caplog):
    """audit() omits user_id when not provided."""
    from jquants_dat_mcp.audit import audit

    with caplog.at_level(logging.INFO, logger="jquants_dat_mcp.audit"):
        audit("health_check")

    assert len(caplog.records) == 1
    entry = json.loads(caplog.records[0].message)
    assert entry["action"] == "health_check"
    assert "user_id" not in entry


def test_audit_extra_fields(caplog):
    """audit() includes arbitrary keyword fields in the log entry."""
    from jquants_dat_mcp.audit import audit

    with caplog.at_level(logging.INFO, logger="jquants_dat_mcp.audit"):
        audit("tool_call", user_id="u1", tool="get_equities_bars_daily", status="ok")

    entry = json.loads(caplog.records[0].message)
    assert entry["tool"] == "get_equities_bars_daily"
    assert entry["status"] == "ok"
