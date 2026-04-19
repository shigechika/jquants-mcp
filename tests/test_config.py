"""Tests for configuration management."""

from __future__ import annotations

import os
from unittest.mock import patch

from jquants_mcp.config import RATE_LIMITS, Settings


def test_default_settings():
    """デフォルト設定値の確認。"""
    import configparser

    # 環境変数・config.ini・toml をすべて無効化してデフォルト値を確認
    env_keys = [
        k
        for k in os.environ
        if k.startswith("JQUANTS_") or k in ("MAX_RETRIES", "RETRY_BASE_DELAY", "MAX_PAGES")
    ]
    with (
        patch.dict(os.environ, {}, clear=False),
        patch("jquants_mcp.config._load_config_files", return_value=configparser.ConfigParser()),
        patch("jquants_mcp.config._read_jquants_toml", return_value=""),
    ):
        for k in env_keys:
            os.environ.pop(k, None)
        s = Settings(jquants_api_key="dummy")
        assert s.jquants_base_url == "https://api.jquants.com/v2"
        assert s.jquants_plan == ""  # デフォルトは空文字列（自動検出）
        assert s.max_retries == 5
        assert s.max_pages == 10


def test_rate_limit_by_plan():
    """プラン別レート制限の確認。"""
    assert RATE_LIMITS["free"] == 5
    assert RATE_LIMITS["light"] == 60
    assert RATE_LIMITS["standard"] == 120
    assert RATE_LIMITS["premium"] == 500


def test_get_rate_limit():
    """Settings.get_rate_limit() の動作確認。"""
    s = Settings(jquants_api_key="dummy", jquants_plan="standard")
    assert s.get_rate_limit() == 120


def test_get_rate_limit_unknown_plan():
    """不明なプランはフリーにフォールバック。"""
    s = Settings(jquants_api_key="dummy", jquants_plan="unknown")
    assert s.get_rate_limit() == RATE_LIMITS["free"]


def test_get_cache_dir(tmp_path):
    """キャッシュディレクトリが作成されること。"""
    cache_dir = tmp_path / "test-cache"
    s = Settings(jquants_api_key="dummy", jquants_cache_dir=str(cache_dir))
    result = s.get_cache_dir()
    assert result == cache_dir
    assert result.exists()


def test_config_ini_loading(tmp_path):
    """config.ini からの設定読み込み。"""
    ini_file = tmp_path / "config.ini"
    ini_file.write_text(
        "[jquants]\napi_key = ini-key\nplan = premium\n\n[client]\nmax_retries = 3\n",
        encoding="utf-8",
    )
    with patch("jquants_mcp.config._load_config_files") as mock_load:
        import configparser

        config = configparser.ConfigParser()
        config.read(str(ini_file))
        mock_load.return_value = config

        s = Settings()
        assert s.jquants_api_key == "ini-key"
        assert s.jquants_plan == "premium"
        assert s.max_retries == 3


def test_env_overrides_config_ini(tmp_path):
    """環境変数が config.ini より優先されること。"""
    ini_file = tmp_path / "config.ini"
    ini_file.write_text("[jquants]\napi_key = ini-key\nplan = light\n", encoding="utf-8")
    with (
        patch("jquants_mcp.config._load_config_files") as mock_load,
        patch.dict(os.environ, {"JQUANTS_API_KEY": "env-key", "JQUANTS_PLAN": "premium"}),
    ):
        import configparser

        config = configparser.ConfigParser()
        config.read(str(ini_file))
        mock_load.return_value = config

        s = Settings()
        assert s.jquants_api_key == "env-key"
        assert s.jquants_plan == "premium"


def test_overrides_take_highest_priority(tmp_path):
    """コンストラクタ引数が最優先されること。"""
    with patch.dict(os.environ, {"JQUANTS_API_KEY": "env-key"}):
        s = Settings(jquants_api_key="override-key")
        assert s.jquants_api_key == "override-key"


def test_reads_api_key_from_jquants_toml(tmp_path):
    """~/.jquants-api/jquants-api.toml から api_key を読み込めること。"""
    toml_file = tmp_path / "jquants-api.toml"
    toml_file.write_bytes(b'[jquants-api-client]\napi_key = "toml-key-123"\n')
    with (
        patch("jquants_mcp.config._JQUANTS_TOML_PATH", toml_file),
        patch("jquants_mcp.config._load_config_files") as mock_load,
    ):
        import configparser

        mock_load.return_value = configparser.ConfigParser()

        s = Settings()
        assert s.jquants_api_key == "toml-key-123"


def test_config_ini_overrides_jquants_toml(tmp_path):
    """config.ini の api_key が jquants-api.toml より優先されること。"""
    toml_file = tmp_path / "jquants-api.toml"
    toml_file.write_bytes(b'[jquants-api-client]\napi_key = "toml-key"\n')
    ini_file = tmp_path / "config.ini"
    ini_file.write_text("[jquants]\napi_key = ini-key\n", encoding="utf-8")
    with (
        patch("jquants_mcp.config._JQUANTS_TOML_PATH", toml_file),
        patch("jquants_mcp.config._load_config_files") as mock_load,
    ):
        import configparser

        config = configparser.ConfigParser()
        config.read(str(ini_file))
        mock_load.return_value = config

        s = Settings()
        assert s.jquants_api_key == "ini-key"


def test_env_overrides_jquants_toml(tmp_path):
    """環境変数が jquants-api.toml より優先されること。"""
    toml_file = tmp_path / "jquants-api.toml"
    toml_file.write_bytes(b'[jquants-api-client]\napi_key = "toml-key"\n')
    with (
        patch("jquants_mcp.config._JQUANTS_TOML_PATH", toml_file),
        patch.dict(os.environ, {"JQUANTS_API_KEY": "env-key"}),
    ):
        s = Settings()
        assert s.jquants_api_key == "env-key"


def test_missing_toml_file_no_error(tmp_path):
    """jquants-api.toml が存在しなくてもエラーにならないこと。"""
    nonexistent = tmp_path / "nonexistent.toml"
    with patch("jquants_mcp.config._JQUANTS_TOML_PATH", nonexistent):
        s = Settings(jquants_api_key="fallback")
        assert s.jquants_api_key == "fallback"


def test_jquants_api_toml_path_env_override(tmp_path):
    """JQUANTS_API_TOML_PATH env overrides the default jquants-api.toml location.

    Motivation: macOS 26+ launchd sandboxing silently blocks open() on
    ~/.jquants-api/jquants-api.toml (mode 600). Users must be able to
    relocate the file to a non-sandboxed path.
    """
    import configparser

    override_file = tmp_path / "override-location" / "jquants-api.toml"
    override_file.parent.mkdir()
    override_file.write_bytes(b'[jquants-api-client]\napi_key = "override-toml-key"\n')

    # Even if the module-level default points at a different toml, env wins.
    decoy = tmp_path / "decoy.toml"
    decoy.write_bytes(b'[jquants-api-client]\napi_key = "decoy-key"\n')

    with (
        patch("jquants_mcp.config._JQUANTS_TOML_PATH", decoy),
        patch.dict(os.environ, {"JQUANTS_API_TOML_PATH": str(override_file)}),
        patch("jquants_mcp.config._load_config_files") as mock_load,
    ):
        mock_load.return_value = configparser.ConfigParser()
        s = Settings()
        assert s.jquants_api_key == "override-toml-key"


def test_jquants_api_toml_path_env_with_tilde(tmp_path, monkeypatch):
    """Tilde in JQUANTS_API_TOML_PATH expands via Path.expanduser()."""
    import configparser

    toml_file = tmp_path / "home" / "custom.toml"
    toml_file.parent.mkdir()
    toml_file.write_bytes(b'[jquants-api-client]\napi_key = "tilde-key"\n')

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JQUANTS_API_TOML_PATH", "~/custom.toml")
    with patch("jquants_mcp.config._load_config_files") as mock_load:
        mock_load.return_value = configparser.ConfigParser()
        s = Settings()
        assert s.jquants_api_key == "tilde-key"
