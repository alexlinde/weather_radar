"""
Two-tier on-disk cache with per-tilt-level folder organisation:

1. Raw cache   — stores .grib2.gz bytes from S3 (avoids re-downloading).
2. Decoded cache — stores clipped numpy grids + metadata (avoids re-decoding).

Layout:
    data/raw/{tilt}/MRMS_...grib2.gz
    data/decoded/{tilt}/MRMS_...npy  + .json
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = _DATA_DIR / "raw"
DECODED_DIR = _DATA_DIR / "decoded"

_LEGACY_RAW_DIR = _DATA_DIR / "grib2_cache"
_LEGACY_DECODED_DIR = _DATA_DIR / "decoded_cache"

_TILT_RE = re.compile(r"MergedReflectivityQC(?!Composite)_(\d{2}\.\d{2})")


def _key_to_path(s3_key: str) -> Path:
    """Extract tilt subfolder and filename from an S3 key.

    'CONUS/MergedReflectivityQC_00.50/20260405/MRMS_...grib2.gz'
    → Path('00.50/MRMS_...grib2.gz')
    """
    parts = s3_key.split("/")
    product = parts[1]  # e.g. 'MergedReflectivityQC_00.50'
    tilt = product.split("_")[-1]
    filename = parts[-1]
    return Path(tilt) / filename


def _decoded_stem(s3_key: str) -> Path:
    """Return the tilt-relative path without the .grib2.gz extension."""
    p = _key_to_path(s3_key)
    name = p.name.removesuffix(".grib2.gz").removesuffix(".grib2")
    return p.parent / name


# ── Raw GRIB2 cache ──────────────────────────────────────────────────────────


def get(s3_key: str) -> bytes | None:
    """Return raw gzipped bytes from disk if cached, else None."""
    path = RAW_DIR / _key_to_path(s3_key)
    if path.exists():
        return path.read_bytes()
    return None


def put(s3_key: str, data: bytes) -> None:
    """Write raw gzipped bytes to disk cache."""
    path = RAW_DIR / _key_to_path(s3_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ── Decoded array cache ───────────────────────────────────────────────────────


def _decoded_paths(s3_key: str) -> tuple[Path, Path]:
    stem = _decoded_stem(s3_key)
    return DECODED_DIR / f"{stem}.npy", DECODED_DIR / f"{stem}.json"


def get_decoded(s3_key: str) -> tuple[np.ndarray, dict[str, Any]] | None:
    """Load a previously decoded + clipped grid from disk."""
    npy_path, json_path = _decoded_paths(s3_key)
    if npy_path.exists() and json_path.exists():
        grid = np.load(npy_path, allow_pickle=False)
        with open(json_path) as f:
            meta = json.load(f)
        return grid, meta
    return None


def put_decoded(s3_key: str, grid: np.ndarray, metadata: dict[str, Any]) -> None:
    """Save a decoded + clipped grid and its metadata to disk."""
    npy_path, json_path = _decoded_paths(s3_key)
    npy_path.parent.mkdir(parents=True, exist_ok=True)

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


# ── Eviction ──────────────────────────────────────────────────────────────────


def evict_older_than(hours: float = 24.0) -> int:
    """Remove cached files older than `hours`. Walks tilt subdirectories."""
    cutoff = time.time() - hours * 3600
    removed = 0
    for top in (RAW_DIR, DECODED_DIR):
        if not top.exists():
            continue
        for path in top.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
    if removed:
        logger.info("Evicted %d stale cache files (>%.0fh old)", removed, hours)
    return removed


# ── Legacy migration ─────────────────────────────────────────────────────────


def migrate_legacy_cache() -> dict[str, int]:
    """Move files from the old flat cache dirs into the new per-tilt layout.

    Returns counts: {'moved': N, 'deleted': N, 'errors': N}
    """
    stats = {"moved": 0, "deleted": 0, "errors": 0}

    for legacy_dir, new_base, suffix in [
        (_LEGACY_RAW_DIR, RAW_DIR, ".grib2.gz"),
        (_LEGACY_DECODED_DIR, DECODED_DIR, ""),
    ]:
        if not legacy_dir.exists():
            continue

        for path in list(legacy_dir.iterdir()):
            if not path.is_file():
                continue

            name = path.name
            if "Composite" in name:
                path.unlink()
                stats["deleted"] += 1
                continue

            m = _TILT_RE.search(name)
            if not m:
                stats["errors"] += 1
                logger.warning("Could not extract tilt from legacy file: %s", name)
                continue

            tilt = m.group(1)

            # The legacy flat name is s3_key.replace("/","_"), e.g.:
            # CONUS_MergedReflectivityQC_00.50_20260405_MRMS_...grib2.gz
            # We want just the MRMS_... portion (everything after the date segment).
            # Pattern: ..._YYYYMMDD_MRMS_... → split on _MRMS_ and reconstruct
            parts = name.split("_MRMS_", 1)
            if len(parts) == 2:
                short_name = "MRMS_" + parts[1]
            else:
                short_name = name

            dest = new_base / tilt / short_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(dest))
                stats["moved"] += 1
            except OSError:
                logger.exception("Failed to move %s → %s", path, dest)
                stats["errors"] += 1

        # Clean up empty legacy directory
        try:
            if legacy_dir.exists() and not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()
                logger.info("Removed empty legacy dir: %s", legacy_dir)
        except OSError:
            pass

    if stats["moved"] or stats["deleted"]:
        logger.info(
            "Legacy cache migration: %d moved, %d deleted (stale composite), %d errors",
            stats["moved"], stats["deleted"], stats["errors"],
        )
    return stats
