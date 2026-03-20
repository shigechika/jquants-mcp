"""SQLite-backed user store with encrypted API key storage."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..models.user import User

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    encrypted_api_key TEXT NOT NULL,
    plan              TEXT NOT NULL DEFAULT 'free',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
)
"""


class UserStore:
    """Persistent store for per-user J-Quants API credentials.

    API keys are encrypted with AES-256-GCM before being written to disk.
    The encryption key is supplied by the caller (derived from config).

    This class uses synchronous sqlite3 (same pattern as CacheStore) since
    individual operations are fast and non-blocking in practice.
    """

    def __init__(self, db_path: Path, encrypt_fn, decrypt_fn) -> None:
        """Initialize the user store.

        Args:
            db_path: Path to the SQLite database file.
            encrypt_fn: Callable[str, str] — encrypts a plaintext API key.
            decrypt_fn: Callable[str, str] — decrypts a stored blob.
        """
        self._db_path = db_path
        self._encrypt = encrypt_fn
        self._decrypt = decrypt_fn
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_DDL)
            conn.commit()
        logger.debug("UserStore initialized at %s", self._db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_user(self, user_id: str) -> User | None:
        """Load a user record by user_id.

        Args:
            user_id: The unique user identifier.

        Returns:
            User with decrypted api_key, or None if not found.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        try:
            api_key = self._decrypt(row["encrypted_api_key"])
        except ValueError:
            logger.error("Failed to decrypt API key for user %s", user_id)
            return None
        return User(
            user_id=row["user_id"],
            api_key=api_key,
            plan=row["plan"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_user(self, user: User) -> None:
        """Insert or update a user record with an encrypted API key.

        Args:
            user: User instance with plain-text api_key (will be encrypted before storage).
        """
        now = int(time.time())
        encrypted = self._encrypt(user.api_key)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, encrypted_api_key, plan, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_api_key = excluded.encrypted_api_key,
                    plan              = excluded.plan,
                    updated_at        = excluded.updated_at
                """,
                (user.user_id, encrypted, user.plan, now, now),
            )
            conn.commit()
        logger.info("Saved API key for user %s (plan=%s)", user.user_id, user.plan)

    def delete_user(self, user_id: str) -> bool:
        """Remove a user record.

        Args:
            user_id: The unique user identifier.

        Returns:
            True if a record was deleted, False if the user was not found.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Deleted user %s", user_id)
        return deleted

    def list_users(self) -> list[str]:
        """Return all registered user_ids (no API keys).

        Returns:
            List of user_id strings.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id FROM users ORDER BY created_at").fetchall()
        return [row["user_id"] for row in rows]
