"""Configuration management for jquants-dat-mcp.

設定の優先順位（後勝ち）:
1. ~/.jquants-api/jquants-api.toml  （J-Quants 公式設定: api_key のみ）
2. ~/.config/jquants-dat-mcp/config.ini  （ユーザーグローバル）
3. ./config.ini                            （カレントディレクトリ）
4. 環境変数                                 （MCP クライアント / CLI）
5. コンストラクタ引数                        （テスト用）
"""

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# プラン別レート制限（リクエスト/分）
RATE_LIMITS: dict[str, int] = {
    "free": 5,
    "light": 60,
    "standard": 120,
    "premium": 500,
}

# config.ini のキー → (セクション, キー名, 環境変数名, デフォルト値)
_CONFIG_DEFS: list[tuple[str, str, str, str, str]] = [
    # (attr_name, section, key, env_var, default)
    ("jquants_api_key", "jquants", "api_key", "JQUANTS_API_KEY", ""),
    (
        "jquants_base_url",
        "jquants",
        "base_url",
        "JQUANTS_BASE_URL",
        "https://api.jquants.com/v2",
    ),
    ("jquants_plan", "jquants", "plan", "JQUANTS_PLAN", "free"),
    ("jquants_cache_dir", "jquants", "cache_dir", "JQUANTS_CACHE_DIR", ""),
    ("max_retries", "client", "max_retries", "MAX_RETRIES", "5"),
    ("retry_base_delay", "client", "retry_base_delay", "RETRY_BASE_DELAY", "1.0"),
    ("max_pages", "client", "max_pages", "MAX_PAGES", "10"),
    ("ssl_certfile", "server", "ssl_certfile", "SSL_CERTFILE", ""),
    ("ssl_keyfile", "server", "ssl_keyfile", "SSL_KEYFILE", ""),
    ("bearer_token", "server", "bearer_token", "MCP_BEARER_TOKEN", ""),
    # GitHub OAuth 2.1 settings
    ("github_client_id", "oauth", "github_client_id", "GITHUB_CLIENT_ID", ""),
    ("github_client_secret", "oauth", "github_client_secret", "GITHUB_CLIENT_SECRET", ""),
    ("oauth_base_url", "oauth", "base_url", "OAUTH_BASE_URL", ""),
    ("oauth_jwt_signing_key", "oauth", "jwt_signing_key", "OAUTH_JWT_SIGNING_KEY", ""),
    ("oauth_require_consent", "oauth", "require_consent", "OAUTH_REQUIRE_CONSENT", "true"),
]

# 型変換テーブル
_TYPE_MAP: dict[str, type] = {
    "max_retries": int,
    "retry_base_delay": float,
    "max_pages": int,
}

# Boolean settings — treated as bool after string conversion
_BOOL_SETTINGS: frozenset[str] = frozenset({"oauth_require_consent"})

# J-Quants 公式設定ファイルのデフォルトパス
_JQUANTS_TOML_PATH = Path.home() / ".jquants-api" / "jquants-api.toml"


def _xdg_config_dir() -> Path:
    """XDG 準拠のグローバル設定ディレクトリを返す。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "jquants-dat-mcp"
    return Path.home() / ".config" / "jquants-dat-mcp"


def _default_cache_dir() -> Path:
    """XDG 準拠のデフォルトキャッシュディレクトリを返す。"""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "jquants-dat-mcp"
    return Path.home() / ".cache" / "jquants-dat-mcp"


def _read_jquants_toml(path: Path | None = None) -> str:
    """~/.jquants-api/jquants-api.toml から api_key を読み取る。"""
    toml_path = path or _JQUANTS_TOML_PATH
    if not toml_path.exists():
        return ""
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        api_key = data.get("jquants-api-client", {}).get("api_key", "")
        if api_key:
            logger.debug("API キーを %s から読み込みました", toml_path)
        return api_key
    except Exception:
        logger.warning("jquants-api.toml の読み込みに失敗しました: %s", toml_path)
        return ""


def _load_config_files(extra_paths: list[str] | None = None) -> configparser.ConfigParser:
    """config.ini ファイルを優先順位に従って読み込む。"""
    config = configparser.ConfigParser()
    search_paths = [
        str(_xdg_config_dir() / "config.ini"),
        "config.ini",
    ]
    if extra_paths:
        search_paths.extend(extra_paths)
    config.read(search_paths, encoding="utf-8")
    return config


class Settings:
    """Configuration settings loaded from jquants-api.toml, config.ini, and env vars."""

    def __init__(self, **overrides: str | int | float) -> None:
        config = _load_config_files()

        # jquants-api.toml から API キーを読み取り（最低優先のフォールバック）
        toml_api_key = _read_jquants_toml()

        for attr, section, key, env_var, default in _CONFIG_DEFS:
            # 優先順位: overrides > env > config.ini > jquants-api.toml > default
            if attr in overrides:
                value = overrides[attr]
            elif os.environ.get(env_var) is not None:
                value = os.environ[env_var]
            else:
                try:
                    value = config.get(section, key)
                except (configparser.NoSectionError, configparser.NoOptionError):
                    # api_key は jquants-api.toml からのフォールバック
                    if attr == "jquants_api_key" and toml_api_key:
                        value = toml_api_key
                    else:
                        value = default

            # 型変換（overrides から直接渡された場合は既に正しい型の可能性あり）
            target_type = _TYPE_MAP.get(attr)
            if target_type and not isinstance(value, target_type):
                value = target_type(value)
            elif attr in _BOOL_SETTINGS and not isinstance(value, bool):
                value = str(value).lower() not in ("false", "0", "no", "off", "")

            setattr(self, attr, value)

    def get_cache_dir(self) -> Path:
        """Return the cache directory path, creating it if needed."""
        d = Path(self.jquants_cache_dir) if self.jquants_cache_dir else _default_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_rate_limit(self) -> int:
        """Return the rate limit (requests/min) for the current plan."""
        return RATE_LIMITS.get(self.jquants_plan.lower(), RATE_LIMITS["free"])
