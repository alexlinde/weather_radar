"""
On-disk cache for raw .grib2.gz files downloaded from S3.

Avoids re-downloading the same file during development restarts.
Files are stored in data/grib2_cache/ with filenames derived from the S3 key.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "grib2_cache"


def _key_to_filename(s3_key: str) -> str:
    """Convert an S3 key like 'CONUS/Product/20260405/file.grib2.gz' to a flat filename."""
    return s3_key.replace("/", "_")


def get(s3_key: str) -> bytes | None:
    """Return raw gzipped bytes from disk if cached, else None."""
    path = CACHE_DIR / _key_to_filename(s3_key)
    if path.exists():
        logger.debug("Disk cache hit: %s", path.name)
        return path.read_bytes()
    return None


def put(s3_key: str, data: bytes) -> None:
    """Write raw gzipped bytes to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / _key_to_filename(s3_key)
    path.write_bytes(data)
    logger.debug("Disk cache write: %s (%d bytes)", path.name, len(data))


def evict_older_than(hours: float = 24.0) -> int:
    """Remove cached files older than `hours`. Returns count of files removed."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - hours * 3600
    removed = 0
    for path in CACHE_DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    if removed:
        logger.info("Evicted %d stale disk cache files (>%.0fh old)", removed, hours)
    return removed
