"""Configuration management for jquants-mcp.

Configuration priority (last wins):
1. ~/.jquants-api/jquants-api.toml  (J-Quants official config: api_key only)
2. ~/.config/jquants-mcp/config.ini  (user global)
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

# Per-plan rate limits (requests/minute).
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


# config.ini key -> (section, key, env var, default value)
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
    # OAuth provider selection ("github" or "google").
    _ConfigDef("oauth_provider", "oauth", "provider", "OAUTH_PROVIDER", "github"),
    # GitHub OAuth 2.1 settings
    _ConfigDef("github_client_id", "oauth", "github_client_id", "GITHUB_CLIENT_ID", ""),
    _ConfigDef("github_client_secret", "oauth", "github_client_secret", "GITHUB_CLIENT_SECRET", ""),
    # Google OAuth 2.0 settings
    _ConfigDef("google_client_id", "oauth", "google_client_id", "GOOGLE_CLIENT_ID", ""),
    _ConfigDef("google_client_secret", "oauth", "google_client_secret", "GOOGLE_CLIENT_SECRET", ""),
    # Common OAuth settings
    _ConfigDef("oauth_base_url", "oauth", "base_url", "OAUTH_BASE_URL", ""),
    _ConfigDef("oauth_jwt_signing_key", "oauth", "jwt_signing_key", "OAUTH_JWT_SIGNING_KEY", ""),
    _ConfigDef(
        "oauth_require_consent", "oauth", "require_consent", "OAUTH_REQUIRE_CONSENT", "true"
    ),
    # Multi-user: encryption key for storing per-user API keys.
    _ConfigDef("encryption_key", "server", "encryption_key", "MCP_ENCRYPTION_KEY", ""),
    # Rotation window: previous encryption key — allowed to decrypt old blobs
    # while the primary key re-encrypts new writes. Leave empty outside a rotation.
    _ConfigDef(
        "encryption_key_previous",
        "server",
        "encryption_key_previous",
        "MCP_ENCRYPTION_KEY_PREVIOUS",
        "",
    ),
    # Per-user rate limiting (multi-user mode only)
    _ConfigDef(
        "rate_limit_per_minute", "server", "rate_limit_per_minute", "RATE_LIMIT_PER_MINUTE", "60"
    ),
    _ConfigDef("rate_limit_burst", "server", "rate_limit_burst", "RATE_LIMIT_BURST", "20"),
    # Email allowlist for restricting Cloud Run access. Comma-separated list.
    # Empty value means "allow any authenticated user" (self-host default).
    _ConfigDef("allowed_emails", "server", "allowed_emails", "JQUANTS_ALLOWED_EMAILS", ""),
    # When true, OAuth users without a registered API key fall back to the
    # global client instead of raising UserNotConfiguredError. Intended for
    # self-hosted deployments where cache.db is pre-populated and read-only
    # tool access should be granted without per-user key registration.
    _ConfigDef("cache_bypass_auth", "server", "cache_bypass_auth", "CACHE_BYPASS_AUTH", "false"),
]

# Type-conversion table
_TYPE_MAP: dict[str, type] = {
    "max_retries": int,
    "retry_base_delay": float,
    "max_pages": int,
    "rate_limit_per_minute": int,
    "rate_limit_burst": int,
}

# Boolean settings — treated as bool after string conversion.
_BOOL_SETTINGS: frozenset[str] = frozenset({"oauth_require_consent", "cache_bypass_auth"})

# J-Quants official config file default path. Tests patch this directly,
# so keep it as a plain module-level constant.
_JQUANTS_TOML_PATH = Path.home() / ".jquants-api" / "jquants-api.toml"


def _jquants_toml_path() -> Path:
    """Return the effective jquants-api.toml path.

    JQUANTS_API_TOML_PATH overrides the default. On macOS 26+ under
    launchd, open() on files inside ~/.jquants-api/ (mode 600) can
    silently hang due to TCC sandbox rules applied to launchd-spawned
    processes; pointing this env var at a non-sandboxed path (e.g.
    /usr/local/etc/jquants-mcp/jquants-api.toml) is the known-good
    workaround.
    """
    override = os.environ.get("JQUANTS_API_TOML_PATH")
    if override:
        return Path(override).expanduser()
    return _JQUANTS_TOML_PATH


def _xdg_config_dir() -> Path:
    """Return the XDG-compliant global configuration directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "jquants-mcp"
    return Path.home() / ".config" / "jquants-mcp"


def _default_cache_dir() -> Path:
    """Return the XDG-compliant default cache directory."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "jquants-mcp"
    return Path.home() / ".cache" / "jquants-mcp"


def _read_jquants_toml(path: Path | None = None) -> str:
    """Read api_key from the J-Quants official config file.

    Honours JQUANTS_API_TOML_PATH when ``path`` is not given so users can
    point at a location outside the launchd sandbox on macOS.
    """
    toml_path = path or _jquants_toml_path()
    if not toml_path.exists():
        return ""
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        api_key = data.get("jquants-api-client", {}).get("api_key", "")
        if api_key:
            logger.debug("Loaded API key from %s", toml_path)
        return api_key
    except Exception:
        logger.warning("Failed to read jquants-api.toml: %s", toml_path)
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

        # Read the API key from jquants-api.toml (lowest-priority fallback).
        toml_api_key = _read_jquants_toml()

        for defn in _CONFIG_DEFS:
            attr, section, key, env_var, default = defn
            # Priority: overrides > env > config.ini > jquants-api.toml > default
            if attr in overrides:
                value = overrides[attr]
            elif os.environ.get(env_var) is not None:
                value = os.environ[env_var]
            else:
                try:
                    value = config.get(section, key)
                except (configparser.NoSectionError, configparser.NoOptionError):
                    # api_key falls back to jquants-api.toml.
                    if attr == "jquants_api_key" and toml_api_key:
                        value = toml_api_key
                    else:
                        value = default

            # Type conversion (values passed directly via overrides may already be the correct type).
            target_type = _TYPE_MAP.get(attr)
            if target_type and not isinstance(value, target_type):
                value = target_type(value)
            elif attr in _BOOL_SETTINGS and not isinstance(value, bool):
                s = str(value).strip().lower()
                if s == "":
                    # An explicitly empty env var (e.g. OAUTH_REQUIRE_CONSENT="")
                    # means "unset" → fall back to the declared default rather
                    # than silently coercing a True-default flag to False.
                    s = str(default).strip().lower()
                value = s not in ("false", "0", "no", "off")

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

    def get_allowed_emails(self) -> list[str]:
        """Return the parsed ``JQUANTS_ALLOWED_EMAILS`` value."""
        from .allowlist import parse_allowed_emails

        return parse_allowed_emails(self.allowed_emails)
