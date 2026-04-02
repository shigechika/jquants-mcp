"""Configuration management for jquants-dat-mcp.

Configuration priority (last wins):
1. ~/.jquants-api/jquants-api.toml  (J-Quants official config: api_key only)
2. ~/.config/jquants-dat-mcp/config.ini  (user global)
3. ./config.ini                            (current directory)
4. Environment variables                   (MCP client / CLI)
5. Constructor arguments                   (for testing)
"""

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path
from typing import NamedTuple

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


class _ConfigDef(NamedTuple):
    """Definition of a single configuration entry."""

    attr: str
    section: str
    key: str
    env_var: str
    default: str


# config.ini のキー → (セクション, キー名, 環境変数名, デフォルト値)
_CONFIG_DEFS: list[_ConfigDef] = [
    # (attr_name, section, key, env_var, default)
    _ConfigDef("jquants_api_key", "jquants", "api_key", "JQUANTS_API_KEY", ""),
    _ConfigDef(
        "jquants_base_url", "jquants", "base_url", "JQUANTS_BASE_URL", "https://api.jquants.com/v2"
    ),
    _ConfigDef("jquants_plan", "jquants", "plan", "JQUANTS_PLAN", ""),
    _ConfigDef("jquants_cache_dir", "jquants", "cache_dir", "JQUANTS_CACHE_DIR", ""),
    _ConfigDef("max_retries", "client", "max_retries", "MAX_RETRIES", "5"),
    _ConfigDef("retry_base_delay", "client", "retry_base_delay", "RETRY_BASE_DELAY", "1.0"),
    _ConfigDef("max_pages", "client", "max_pages", "MAX_PAGES", "10"),
    _ConfigDef("ssl_certfile", "server", "ssl_certfile", "SSL_CERTFILE", ""),
    _ConfigDef("ssl_keyfile", "server", "ssl_keyfile", "SSL_KEYFILE", ""),
    _ConfigDef("bearer_token", "server", "bearer_token", "MCP_BEARER_TOKEN", ""),
    # OAuth プロバイダー選択（"github" or "google"）
    _ConfigDef("oauth_provider", "oauth", "provider", "OAUTH_PROVIDER", "github"),
    # GitHub OAuth 2.1 設定
    _ConfigDef("github_client_id", "oauth", "github_client_id", "GITHUB_CLIENT_ID", ""),
    _ConfigDef("github_client_secret", "oauth", "github_client_secret", "GITHUB_CLIENT_SECRET", ""),
    # Google OAuth 2.0 設定
    _ConfigDef("google_client_id", "oauth", "google_client_id", "GOOGLE_CLIENT_ID", ""),
    _ConfigDef("google_client_secret", "oauth", "google_client_secret", "GOOGLE_CLIENT_SECRET", ""),
    # 共通 OAuth settings
    _ConfigDef("oauth_base_url", "oauth", "base_url", "OAUTH_BASE_URL", ""),
    _ConfigDef("oauth_jwt_signing_key", "oauth", "jwt_signing_key", "OAUTH_JWT_SIGNING_KEY", ""),
    _ConfigDef(
        "oauth_require_consent", "oauth", "require_consent", "OAUTH_REQUIRE_CONSENT", "true"
    ),
    # マルチユーザー: ユーザーごとの API キー保存用暗号化キー
    _ConfigDef("encryption_key", "server", "encryption_key", "MCP_ENCRYPTION_KEY", ""),
]

# 型変換テーブル
_TYPE_MAP: dict[str, type] = {
    "max_retries": int,
    "retry_base_delay": float,
    "max_pages": int,
}

# 真偽値設定 — 文字列変換後に bool として扱う
_BOOL_SETTINGS: frozenset[str] = frozenset({"oauth_require_consent"})

# J-Quants 公式設定ファイルのデフォルトパス
_JQUANTS_TOML_PATH = Path.home() / ".jquants-api" / "jquants-api.toml"


def _xdg_config_dir() -> Path:
    """Return the XDG-compliant global configuration directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "jquants-dat-mcp"
    return Path.home() / ".config" / "jquants-dat-mcp"


def _default_cache_dir() -> Path:
    """Return the XDG-compliant default cache directory."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "jquants-dat-mcp"
    return Path.home() / ".cache" / "jquants-dat-mcp"


def _read_jquants_toml(path: Path | None = None) -> str:
    """Read api_key from ~/.jquants-api/jquants-api.toml."""
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
    """Load config.ini files according to priority order."""
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

        for defn in _CONFIG_DEFS:
            attr, section, key, env_var, default = defn
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

        self.__post_init_validate()

    def __post_init_validate(self) -> None:
        """Validate numeric settings after construction (called from __init__)."""
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.max_retries > 20:
            logger.warning("max_retries=%d is unusually high (>20)", self.max_retries)
        if self.max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {self.max_pages}")
        if self.retry_base_delay <= 0:
            raise ValueError(f"retry_base_delay must be > 0, got {self.retry_base_delay}")

    def get_cache_dir(self) -> Path:
        """Return the cache directory path, creating it if needed."""
        d = Path(self.jquants_cache_dir) if self.jquants_cache_dir else _default_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_cache_db_path(self) -> Path:
        """Return the cache database file path."""
        return self.get_cache_dir() / "cache.db"

    def get_rate_limit(self) -> int:
        """Return the rate limit (requests/min) for the current plan."""
        return RATE_LIMITS.get(self.jquants_plan.lower(), RATE_LIMITS["free"])
