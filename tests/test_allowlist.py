"""Tests for the email allowlist that gates multi-user access (#107)."""

from __future__ import annotations

from jquants_mcp.allowlist import (
    is_user_allowed,
    parse_allowed_emails,
    unauthorized_message,
)
from jquants_mcp.config import Settings
from jquants_mcp.exceptions import UserNotAllowedError


class TestParseAllowedEmails:
    def test_empty_returns_empty_list(self):
        assert parse_allowed_emails("") == []

    def test_single_email(self):
        assert parse_allowed_emails("alice@example.com") == ["alice@example.com"]

    def test_multiple_emails(self):
        assert parse_allowed_emails("alice@x.com,bob@y.com") == [
            "alice@x.com",
            "bob@y.com",
        ]

    def test_trims_whitespace(self):
        assert parse_allowed_emails(" alice@x.com ,  bob@y.com ") == [
            "alice@x.com",
            "bob@y.com",
        ]

    def test_lowercases(self):
        assert parse_allowed_emails("Alice@Example.COM") == ["alice@example.com"]

    def test_skips_empty_entries(self):
        # Trailing comma / consecutive commas should not create empty strings.
        assert parse_allowed_emails("alice@x.com,,bob@y.com,") == [
            "alice@x.com",
            "bob@y.com",
        ]


class TestIsUserAllowed:
    def test_empty_allowlist_allows_any(self):
        # Self-host default: no restriction.
        assert is_user_allowed("anyone@example.com", []) is True

    def test_allowed_email_returns_true(self):
        assert is_user_allowed("alice@x.com", ["alice@x.com", "bob@y.com"]) is True

    def test_unallowed_email_returns_false(self):
        assert is_user_allowed("mallory@x.com", ["alice@x.com"]) is False

    def test_case_insensitive(self):
        # OAuth providers sometimes return mixed-case email domains; match must
        # be case-insensitive so the allowlist does not become accidentally strict.
        assert is_user_allowed("Alice@Example.COM", ["alice@example.com"]) is True


class TestUnauthorizedMessage:
    def test_includes_user_email(self):
        msg = unauthorized_message("mallory@evil.com")
        assert "mallory@evil.com" in msg

    def test_points_to_self_host_path(self):
        # The rejection message must help legitimate would-be users figure
        # out the right path forward (run their own instance).
        msg = unauthorized_message("x@y.com")
        assert "self" in msg.lower() or "own instance" in msg.lower()
        assert "README" in msg or "readme" in msg.lower()


class TestUserNotAllowedError:
    def test_is_subclass_of_base_mcp_error(self):
        # Tool layer catches JQuantsDatMCPError family; must inherit so
        # existing except clauses pick it up.
        from jquants_mcp.exceptions import JQuantsDatMCPError

        assert issubclass(UserNotAllowedError, JQuantsDatMCPError)

    def test_to_dict_shape(self):
        err = UserNotAllowedError("mallory@evil.com")
        d = err.to_dict()
        assert d["error"] is True
        assert d["error_type"] == "UserNotAllowedError"
        assert "mallory@evil.com" in d["message"]
        assert "hint" in d  # Guides the user to self-host.

    def test_stores_user_id(self):
        err = UserNotAllowedError("alice@x.com")
        assert err.user_id == "alice@x.com"


class TestSettingsGetAllowedEmails:
    def test_default_is_empty(self, monkeypatch, tmp_path):
        # Isolate from real env / HOME config.
        monkeypatch.delenv("JQUANTS_ALLOWED_EMAILS", raising=False)
        monkeypatch.setattr(
            "jquants_mcp.config._load_config_files",
            lambda *a, **kw: _empty_config(),
        )
        monkeypatch.setattr("jquants_mcp.config._read_jquants_toml", lambda *a, **kw: "")
        settings = Settings()
        assert settings.get_allowed_emails() == []

    def test_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("JQUANTS_ALLOWED_EMAILS", "alice@x.com, Bob@Y.COM")
        monkeypatch.setattr(
            "jquants_mcp.config._load_config_files",
            lambda *a, **kw: _empty_config(),
        )
        monkeypatch.setattr("jquants_mcp.config._read_jquants_toml", lambda *a, **kw: "")
        settings = Settings()
        assert settings.get_allowed_emails() == ["alice@x.com", "bob@y.com"]


def _empty_config():
    import configparser

    return configparser.ConfigParser()
