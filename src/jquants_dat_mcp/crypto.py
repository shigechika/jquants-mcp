"""AES-256-GCM encryption utilities for storing sensitive values at rest."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

_SALT_BYTES = 16  # 128ビットランダムソルト、暗号化ごとに生成
_IV_BYTES = 12  # AES-GCM 用 96ビット IV
_ITERATIONS = 200_000

# 新 blob フォーマットのバージョンマーカー（salt+iv+ct の前に1バイト付加）
_FORMAT_V2 = b"\x02"

# レガシー固定ソルト — この変更前に作成された古い blob の復号のためにのみ保持
_LEGACY_SALT = b"jquants-dat-mcp-v1"


def _pbkdf2(passphrase: bytes, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from passphrase + salt using PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_ITERATIONS,
    )
    return kdf.derive(passphrase)


def derive_key(passphrase: str | bytes, salt: bytes = _LEGACY_SALT) -> bytes:
    """Derive a 256-bit AES key from an arbitrary passphrase using PBKDF2-HMAC-SHA256.

    Kept for backward compatibility and direct testing of key derivation.
    Production code should use encrypt()/decrypt() which handle salt internally.

    Args:
        passphrase: A secret string or bytes used as the master key material.
        salt: KDF salt bytes. Defaults to the legacy fixed salt.

    Returns:
        32-byte AES-256 key.
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    return _pbkdf2(passphrase, salt)


def encrypt(plaintext: str, passphrase: str | bytes) -> str:
    """Encrypt a plaintext string with AES-256-GCM using a random per-message salt.

    Output blob format: base64url( FORMAT_V2(1) || SALT(16) || IV(12) || CT+TAG )

    Each call generates a fresh random salt, so two encryptions of the same
    plaintext always produce distinct ciphertext blobs.

    Args:
        plaintext: The string to encrypt.
        passphrase: Secret passphrase from which the encryption key is derived.

    Returns:
        Base64url-encoded ciphertext blob.
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    salt = os.urandom(_SALT_BYTES)
    iv = os.urandom(_IV_BYTES)
    key = _pbkdf2(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return base64.urlsafe_b64encode(_FORMAT_V2 + salt + iv + ciphertext).decode()


def decrypt(blob: str, passphrase: str | bytes) -> str:
    """Decrypt an AES-256-GCM ciphertext blob produced by encrypt().

    Supports both the current format (random salt, FORMAT_V2 marker) and the
    legacy format (fixed salt, no version marker) for backward compatibility
    with blobs created before the random-salt change.

    Detection logic:
    - New blob: first raw byte == FORMAT_V2 (0x02) AND length >= 45 bytes
    - Legacy blob: anything else → IV is the first 12 bytes, key from legacy salt

    In the rare case (~1/256) that a legacy blob's first byte happens to equal
    FORMAT_V2, the new-format attempt is tried first; if it fails (wrong GCM
    tag), the legacy path is used as a fallback.

    Args:
        blob: Base64url-encoded ciphertext blob.
        passphrase: Secret passphrase used during encryption.

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If decryption fails (wrong passphrase, tampered data, bad blob).
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    try:
        raw = base64.urlsafe_b64decode(blob.encode() + b"==")

        # 新フォーマットを試行: FORMAT_V2(1) || SALT(16) || IV(12) || CT+TAG（最小45バイト）
        min_v2_len = 1 + _SALT_BYTES + _IV_BYTES + 16  # 16 = GCM タグ+CT の最小サイズ
        if raw[:1] == _FORMAT_V2 and len(raw) >= min_v2_len:
            salt = raw[1 : 1 + _SALT_BYTES]
            iv = raw[1 + _SALT_BYTES : 1 + _SALT_BYTES + _IV_BYTES]
            ct = raw[1 + _SALT_BYTES + _IV_BYTES :]
            try:
                return AESGCM(_pbkdf2(passphrase, salt)).decrypt(iv, ct, None).decode()
            except Exception:
                # レガシー blob とのまれなバージョンバイト衝突 — レガシー処理にフォールスルー
                pass

        # レガシーフォーマット: IV(12) || CT+TAG（固定ソルトから鍵導出）
        iv = raw[:_IV_BYTES]
        ct = raw[_IV_BYTES:]
        return AESGCM(_pbkdf2(passphrase, _LEGACY_SALT)).decrypt(iv, ct, None).decode()

    except Exception as exc:
        raise ValueError("Decryption failed — wrong key or corrupted data") from exc
