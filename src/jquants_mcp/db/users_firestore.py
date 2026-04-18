"""Firestore-backed user store with encrypted API key storage.

Used on Cloud Run where SQLite + GCS sync is not viable due to
instance lifecycle constraints. Shares the same interface as
``SQLiteUserStore`` (db/users.py) so the server code can treat them
interchangeably.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..models.user import User

logger = logging.getLogger(__name__)

_COLLECTION = "users"


class FirestoreUserStore:
    """Persistent store for per-user J-Quants API credentials, backed by Firestore.

    API keys are encrypted with AES-256-GCM before being written. The
    encryption key is supplied by the caller. All Cloud Run instances
    share the same Firestore database in real time — no sync needed.
    """

    def __init__(
        self,
        project: str,
        encrypt_fn: Callable[[str], str],
        decrypt_fn: Callable[[str], str],
        *,
        collection: str = _COLLECTION,
    ) -> None:
        from google.cloud import firestore  # type: ignore[import-untyped]

        self._encrypt = encrypt_fn
        self._decrypt = decrypt_fn
        self._client = firestore.Client(project=project)
        self._collection = self._client.collection(collection)
        logger.debug(
            "FirestoreUserStore initialized (project=%s collection=%s)", project, collection
        )

    def _doc(self, user_id: str):
        return self._collection.document(user_id)

    def get_user(self, user_id: str) -> User | None:
        snap = self._doc(user_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        try:
            api_key = self._decrypt(data["encrypted_api_key"])
        except Exception:
            logger.error(
                "Failed to decrypt API key for user %s — encryption key may have changed",
                user_id,
            )
            return None
        return User(
            user_id=user_id,
            api_key=api_key,
            plan=data.get("plan", "free"),
            created_at=int(data.get("created_at", 0)),
            updated_at=int(data.get("updated_at", 0)),
            last_validated_at=data.get("last_validated_at"),
        )

    def has_corrupted_key(self, user_id: str) -> bool:
        snap = self._doc(user_id).get()
        if not snap.exists:
            return False
        data = snap.to_dict() or {}
        try:
            self._decrypt(data.get("encrypted_api_key", ""))
            return False
        except Exception:
            return True

    def save_user(self, user: User) -> None:
        now = int(time.time())
        encrypted = self._encrypt(user.api_key)
        doc_ref = self._doc(user.user_id)
        snap = doc_ref.get()
        if snap.exists:
            doc_ref.update(
                {
                    "encrypted_api_key": encrypted,
                    "plan": user.plan,
                    "updated_at": now,
                }
            )
        else:
            doc_ref.set(
                {
                    "encrypted_api_key": encrypted,
                    "plan": user.plan,
                    "created_at": now,
                    "updated_at": now,
                    "last_validated_at": None,
                }
            )
        logger.info("Saved API key for user %s (plan=%s)", user.user_id, user.plan)

    def delete_user(self, user_id: str) -> bool:
        doc_ref = self._doc(user_id)
        snap = doc_ref.get()
        if not snap.exists:
            return False
        doc_ref.delete()
        logger.info("Deleted user %s", user_id)
        return True

    def update_last_validated(self, user_id: str) -> None:
        now = int(time.time())
        self._doc(user_id).update({"last_validated_at": now})

    def update_plan(self, user_id: str, plan: str) -> None:
        now = int(time.time())
        self._doc(user_id).update({"plan": plan, "updated_at": now})
        logger.info("Updated plan for user %s to %s", user_id, plan)

    def list_users(self) -> list[str]:
        docs = self._collection.order_by("created_at").stream()
        return [doc.id for doc in docs]
