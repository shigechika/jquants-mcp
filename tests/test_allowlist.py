"""Tests for the email allowlist that gates multi-user access (#107)."""

from __future__ import annotations

from types import SimpleNamespace

from jquants_mcp.allowlist import (
    get_user_email,
    is_email_allowed,
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


class TestGetUserEmail:
    def test_returns_email_from_claims(self):
        # FastMCP's GoogleProvider populates token.claims["email"].
        token = SimpleNamespace(
            client_id="100526143775213853355",  # Google sub (numeric)
            claims={"sub": "100526143775213853355", "email": "alice@example.com"},
        )
        assert get_user_email(token) == "alice@example.com"

    def test_lowercases(self):
        # Google occasionally returns mixed-case email; the allowlist
        # comparison is case-insensitive so normalize here too.
        token = SimpleNamespace(claims={"email": "Alice@Example.COM"})
        assert get_user_email(token) == "alice@example.com"

    def test_missing_claims_attr_returns_none(self):
        # The base SDK AccessToken (without FastMCP's subclass) has no
        # `claims` attribute. Defensive: should not crash.
        token = SimpleNamespace(client_id="bearer")
        assert get_user_email(token) is None

    def test_missing_email_claim_returns_none(self):
        token = SimpleNamespace(claims={"sub": "12345"})
        assert get_user_email(token) is None

    def test_empty_email_returns_none(self):
        token = SimpleNamespace(claims={"email": ""})
        assert get_user_email(token) is None

    def test_email_verified_false_drops_email(self):
        # Defense-in-depth: an explicitly unverified email is dropped so
        # an attacker cannot claim someone else's address by signing up
        # with email verification disabled.
        token = SimpleNamespace(claims={"email": "alice@example.com", "email_verified": False})
        assert get_user_email(token) is None

    def test_email_verified_missing_still_returns_email(self):
        # GitHub does not populate `email_verified`. Treat absence as
        # "trust the email" so GitHub users are not blanket-blocked.
        token = SimpleNamespace(claims={"email": "alice@example.com"})
        assert get_user_email(token) == "alice@example.com"

    def test_email_verified_true_returns_email(self):
        token = SimpleNamespace(claims={"email": "alice@example.com", "email_verified": True})
        assert get_user_email(token) == "alice@example.com"


class TestIsEmailAllowed:
    def test_empty_allowlist_allows_any(self):
        assert is_email_allowed("alice@x.com", []) is True
        assert is_email_allowed(None, []) is True

    def test_allowed_returns_true(self):
        assert is_email_allowed("alice@x.com", ["alice@x.com", "bob@y.com"]) is True

    def test_unallowed_returns_false(self):
        assert is_email_allowed("mallory@x.com", ["alice@x.com"]) is False

    def test_case_insensitive(self):
        assert is_email_allowed("Alice@Example.COM", ["alice@example.com"]) is True

    def test_none_email_with_nonempty_allowlist_fails_closed(self):
        # If the OAuth token did not carry an email but the deployment
        # has an allowlist configured, deny rather than allow. Without
        # an email claim we cannot prove the user is on the list.
        assert is_email_allowed(None, ["alice@x.com"]) is False
        assert is_email_allowed("", ["alice@x.com"]) is False


class TestRegressionGoogleSubVsEmail:
    """Pre-fix regression: the gate was passing the Google sub (numeric)
    as if it were the email, so allowlist always rejected."""

    def test_numeric_sub_is_not_treated_as_email(self):
        google_sub = "100526143775213853355"
        # The new check returns False for the sub against an email
        # allowlist, but combined with the email extraction the call
        # site no longer passes the sub at all.
        assert is_email_allowed(google_sub, ["user@example.com"]) is False


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
