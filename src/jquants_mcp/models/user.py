"""User model for multi-user J-Quants API key management."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import NamedTuple


class UserMeta(NamedTuple):
    """Lightweight user metadata read without decrypting the API key.

    Returned by ``get_user_meta`` on the hot path to reuse a cached client
    without paying the PBKDF2 key-derivation cost of a full ``get_user``.
    """

    plan: str
    last_validated_at: int | None


@dataclass
class User:
    """A registered user with their J-Quants API credentials.

    API keys are stored encrypted at rest in the user database.
    The plain-text api_key is only present when freshly loaded/created.
    """

    user_id: str
    """Unique user identifier (e.g. GitHub numeric user ID as string)."""

    api_key: str
    """Plain-text J-Quants API key (decrypted on load, never persisted directly)."""

    plan: str = "free"
    """J-Quants subscription plan (free | light | standard | premium)."""

    created_at: int = field(default_factory=lambda: int(time()))
    updated_at: int = field(default_factory=lambda: int(time()))

    last_validated_at: int | None = None
    """Unix timestamp of the last successful API key validation. None if never validated."""
