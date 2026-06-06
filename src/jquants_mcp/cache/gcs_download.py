"""Shared helper for downloading the zstd-compressed cache.db from GCS.

Both the Cloud Run startup copy (``scripts/gcs_sync.py``) and the Pub/Sub
reload (``server.py``) fetch the same ``cache.db.zst`` object. Keeping the
stream-decompress logic in one place avoids the duplicated-download drift that
previously caused a temp-file collision between the two paths.

Imports of ``zstandard`` and ``google.cloud`` are deferred into the function so
this module stays importable (and cheap) without the ``cloud-run`` extra.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def stream_download_zst(bucket, zst_blob_name: str, dest: Path) -> bool:
    """Stream-download a zstd object from GCS and decompress it to ``dest``.

    Streaming (GCS read -> zstd decompress -> file) keeps the tmpfs/RAM peak at
    just the decompressed output — the full compressed object is never staged —
    so a warm-instance reload stays within the same 2x-cache.db budget as the
    uncompressed path.

    Args:
        bucket: A ``google.cloud.storage`` bucket handle.
        zst_blob_name: Object name of the compressed blob (e.g.
            ``"jquants-mcp/cache.db.zst"``).
        dest: Local path to write the decompressed bytes to.

    Returns:
        True on success. False when zstandard is unavailable, the object is
        missing, or decompression fails — in every False case the caller should
        fall back to the uncompressed object, so a missing/old ``.zst`` never
        breaks startup or a reload.
    """
    try:
        import zstandard
    except ImportError:
        logger.info("zstandard not installed; falling back to uncompressed cache.db")
        return False

    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    blob = bucket.blob(zst_blob_name)
    try:
        dctx = zstandard.ZstdDecompressor()
        with blob.open("rb") as src, open(dest, "wb") as out:
            dctx.copy_stream(src, out)
        return True
    except NotFound:
        logger.info("%s not found; falling back to uncompressed cache.db", zst_blob_name)
        dest.unlink(missing_ok=True)
        return False
    except Exception as exc:
        logger.warning(
            "zstd download/decompress of %s failed (%s); falling back to uncompressed",
            zst_blob_name,
            exc,
        )
        dest.unlink(missing_ok=True)
        return False
