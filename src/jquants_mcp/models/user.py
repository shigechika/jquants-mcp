"""User model for multi-user J-Quants API key management."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time


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
