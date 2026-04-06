"""
Two-tier on-disk cache:

1. Raw cache   — stores .grib2.gz bytes from S3 (avoids re-downloading).
2. Decoded cache — stores clipped numpy grids + metadata (avoids re-decoding).

Both live under data/ with separate subdirectories.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = _DATA_DIR / "grib2_cache"
DECODED_DIR = _DATA_DIR / "decoded_cache"


def _key_to_filename(s3_key: str) -> str:
    """Convert an S3 key like 'CONUS/Product/20260405/file.grib2.gz' to a flat filename."""
    return s3_key.replace("/", "_")


# ── Raw GRIB2 cache ──────────────────────────────────────────────────────────


def get(s3_key: str) -> bytes | None:
    """Return raw gzipped bytes from disk if cached, else None."""
    path = CACHE_DIR / _key_to_filename(s3_key)
    if path.exists():
        return path.read_bytes()
    return None


def put(s3_key: str, data: bytes) -> None:
    """Write raw gzipped bytes to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / _key_to_filename(s3_key)
    path.write_bytes(data)


# ── Decoded array cache ───────────────────────────────────────────────────────


def _decoded_paths(s3_key: str) -> tuple[Path, Path]:
    base = _key_to_filename(s3_key).removesuffix(".grib2.gz").removesuffix(".grib2")
    return DECODED_DIR / f"{base}.npy", DECODED_DIR / f"{base}.json"


def get_decoded(s3_key: str) -> tuple[np.ndarray, dict[str, Any]] | None:
    """Load a previously decoded + clipped grid from disk. Returns (grid, metadata) or None."""
    npy_path, json_path = _decoded_paths(s3_key)
    if npy_path.exists() and json_path.exists():
        grid = np.load(npy_path, allow_pickle=False)
        with open(json_path) as f:
            meta = json.load(f)
        return grid, meta
    return None


def put_decoded(s3_key: str, grid: np.ndarray, metadata: dict[str, Any]) -> None:
    """Save a decoded + clipped grid and its metadata to disk."""
    DECODED_DIR.mkdir(parents=True, exist_ok=True)
    npy_path, json_path = _decoded_paths(s3_key)

    np.save(npy_path, grid.astype(np.float32))

    serialisable = {}
    for k, v in metadata.items():
        if isinstance(v, (np.integer, np.floating)):
            serialisable[k] = v.item()
        elif isinstance(v, np.bool_):
            serialisable[k] = bool(v)
        else:
            serialisable[k] = v
    with open(json_path, "w") as f:
        json.dump(serialisable, f)


# ── Eviction (covers both tiers) ─────────────────────────────────────────────


def evict_older_than(hours: float = 24.0) -> int:
    """Remove cached files older than `hours` from both tiers. Returns total removed."""
    cutoff = time.time() - hours * 3600
    removed = 0
    for d in (CACHE_DIR, DECODED_DIR):
        if not d.exists():
            continue
        for path in d.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
    if removed:
        logger.info("Evicted %d stale cache files (>%.0fh old)", removed, hours)
    return removed
