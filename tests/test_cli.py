"""Tests for the CLI entry point (jquants_mcp.cli)."""

from __future__ import annotations

import configparser
import os
import stat
from unittest.mock import patch

import jquants_mcp.cli as cli


class TestServeArgs:
    def test_host_defaults_to_loopback(self):
        """Security requirement: --host defaults to 127.0.0.1, not 0.0.0.0."""
        captured = {}

        def fake_run_server(**kwargs):
            captured.update(kwargs)

        with patch("jquants_mcp.server.run_server", fake_run_server):
            rc = cli.main([])
        assert rc == 0
        assert captured["host"] == "127.0.0.1"

    def test_explicit_host_is_passed_through(self):
        captured = {}

        with patch("jquants_mcp.server.run_server", lambda **kw: captured.update(kw)):
            cli.main(["--host", "0.0.0.0", "--port", "9001"])
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 9001

    def test_transport_default_is_stdio(self):
        captured = {}
        with patch("jquants_mcp.server.run_server", lambda **kw: captured.update(kw)):
            cli.main([])
        assert captured["transport"] == "stdio"


class TestWriteApiKey:
    def test_sets_0600_permissions_on_posix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = cli._write_api_key("secret-key-123")
        assert path.exists()
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_key_is_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cli._write_api_key("secret-key-123")
        cfg = configparser.ConfigParser()
        cfg.read(cli._config_ini_path(), encoding="utf-8")
        assert cfg["jquants"]["api_key"] == "secret-key-123"

    def test_preserves_existing_sections(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = cli._config_ini_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        pre = configparser.ConfigParser()
        pre["other"] = {"keep": "me"}
        with open(path, "w", encoding="utf-8") as f:
            pre.write(f)

        cli._write_api_key("new-key")
        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")
        assert cfg["other"]["keep"] == "me"
        assert cfg["jquants"]["api_key"] == "new-key"


class TestLogout:
    def test_logout_removes_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cli._write_api_key("to-be-cleared")
        rc = cli.main(["logout"])
        assert rc == 0
        cfg = configparser.ConfigParser()
        cfg.read(cli._config_ini_path(), encoding="utf-8")
        assert "api_key" not in cfg["jquants"]

    def test_logout_when_no_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = cli.main(["logout"])
        assert rc == 0


class TestLogin:
    def test_login_saves_returned_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        class _Result:
            api_key = "logged-in-key"

        with patch("jquants_mcp.oauth_login.perform_login", return_value=_Result()):
            rc = cli.main(["login", "--no-browser"])
        assert rc == 0
        cfg = configparser.ConfigParser()
        cfg.read(cli._config_ini_path(), encoding="utf-8")
        assert cfg["jquants"]["api_key"] == "logged-in-key"

    def test_login_failure_returns_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from jquants_mcp.oauth_login import LoginError

        with patch(
            "jquants_mcp.oauth_login.perform_login",
            side_effect=LoginError("boom"),
        ):
            rc = cli.main(["login"])
        assert rc == 1
