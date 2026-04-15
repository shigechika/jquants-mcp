#!/usr/bin/env python3
"""Re-encrypt all stored user API keys with a new MCP_ENCRYPTION_KEY.

Intended for a planned key rotation. Run **after** the new key has been
deployed to Cloud Run as ``MCP_ENCRYPTION_KEY`` with the previous key
still set as ``MCP_ENCRYPTION_KEY_PREVIOUS`` — the dual-key decrypt path
keeps the service serving during the migration.

Flow:
  1. Decrypt each ``encrypted_api_key`` with the **old** key
  2. Re-encrypt with the **new** key
  3. Write back in a single Firestore document update
  4. Print a progress line per user and a final summary

Idempotent: re-running is safe — any document already encrypted with the
new key is simply re-encrypted again with the same new key.

Usage:
  OLD_KEY=$(gcloud secrets versions access 1 --secret=mcp-encryption-key)
  NEW_KEY=$(gcloud secrets versions access latest --secret=mcp-encryption-key)

  uv run python scripts/rotate_encryption_key.py \\
      --project aikawa-dx \\
      --old-key "$OLD_KEY" \\
      --new-key "$NEW_KEY" \\
      [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from jquants_dat_mcp.crypto import decrypt_with_fallback, encrypt

if TYPE_CHECKING:
    from google.cloud import firestore  # type: ignore[import-untyped]


def rotate(
    *,
    project: str,
    old_key: str,
    new_key: str,
    collection: str = "users",
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Re-encrypt every user's API key with ``new_key``.

    Returns (success, skipped, failed) counts.
    """
    from google.cloud import firestore  # type: ignore[import-untyped]

    client = firestore.Client(project=project)
    coll = client.collection(collection)

    candidates = [new_key, old_key]  # primary first so already-rotated docs stay cheap
    success = skipped = failed = 0

    for snap in coll.stream():
        user_id = snap.id
        data = snap.to_dict() or {}
        blob = data.get("encrypted_api_key")
        if not blob:
            print(f"  skip: {user_id} (no encrypted_api_key field)")
            skipped += 1
            continue

        try:
            plaintext = decrypt_with_fallback(blob, candidates)
        except ValueError as exc:
            print(f"  FAIL: {user_id}: {exc}", file=sys.stderr)
            failed += 1
            continue

        new_blob = encrypt(plaintext, new_key)
        if dry_run:
            print(f"  would rotate: {user_id}")
        else:
            snap.reference.update({"encrypted_api_key": new_blob})
            print(f"  rotated: {user_id}")
        success += 1

    return success, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True, help="GCP project id")
    ap.add_argument("--old-key", required=True, help="previous MCP_ENCRYPTION_KEY")
    ap.add_argument("--new-key", required=True, help="new MCP_ENCRYPTION_KEY")
    ap.add_argument("--collection", default="users")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.old_key == args.new_key:
        print("old and new keys are identical — nothing to do", file=sys.stderr)
        return 1

    print(
        f"Rotating collection '{args.collection}' in project {args.project}"
        + (" (dry-run)" if args.dry_run else "")
    )
    success, skipped, failed = rotate(
        project=args.project,
        old_key=args.old_key,
        new_key=args.new_key,
        collection=args.collection,
        dry_run=args.dry_run,
    )
    print(f"\nsummary: success={success} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
