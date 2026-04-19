"""
On-disk cache with per-tilt-level folder organisation:

1. Raw cache       — stores .grib2.gz bytes from S3 (avoids re-downloading).
2. Tilt grid cache — stores sparse CSR matrices per tilt per timestamp (avoids re-decoding).

Layout:
    data/raw/{tilt}/MRMS_...grib2.gz
    data/tilt_grids/{YYYYMMDD-HHMMSS}/{tilt}.npz  + meta.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import scipy.sparse as sp

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
RAW_DIR = _DATA_DIR / "raw"
TILT_GRIDS_DIR = _DATA_DIR / "tilt_grids"

_LEGACY_RAW_DIR = _DATA_DIR / "grib2_cache"
_LEGACY_DECODED_DIR = _DATA_DIR / "decoded_cache"
_LEGACY_DECODED_DIR2 = _DATA_DIR / "decoded"
_LEGACY_COMPOSITES_DIR = _DATA_DIR / "composites"

_TILT_RE = re.compile(r"MergedReflectivityQC(?!Composite)_(\d{2}\.\d{2})")


def _key_to_path(s3_key: str) -> Path:
    """Extract tilt subfolder and filename from an S3 key."""
    parts = s3_key.split("/")
    product = parts[1]
    tilt = product.split("_")[-1]
    filename = parts[-1]
    return Path(tilt) / filename


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


# ── Tilt grid cache (scipy.sparse CSR) ───────────────────────────────────────


def _ts_to_stem(timestamp: str) -> str:
    """Convert an ISO timestamp to a filesystem-safe stem: ``YYYYMMDD-HHMMSS``."""
    return (
        timestamp
        .replace(":", "")
        .replace("-", "")
        .replace("T", "-")
        .replace(" ", "-")
        .split("+")[0]
        .rstrip("Z")
    )


def _serialise_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    """Convert numpy scalars to Python primitives for JSON serialisation."""
    out: dict[str, Any] = {}
    for k, v in metadata.items():
        if isinstance(v, (np.integer, np.floating)):
            out[k] = v.item()
        elif isinstance(v, np.bool_):
            out[k] = bool(v)
        else:
            out[k] = v
    return out


def put_tilt_grids(
    timestamp: str,
    sparse_grids: dict[str, sp.csr_matrix],
    metadata: dict[str, Any],
) -> None:
    """Save all tilt grids for one timestamp as individual sparse .npz files."""
    stem = _ts_to_stem(timestamp)
    ts_dir = TILT_GRIDS_DIR / stem
    ts_dir.mkdir(parents=True, exist_ok=True)

    for tilt, matrix in sparse_grids.items():
        sp.save_npz(ts_dir / f"{tilt}.npz", matrix)

    meta = _serialise_meta(metadata)
    meta["timestamp"] = timestamp
    with open(ts_dir / "meta.json", "w") as f:
        json.dump(meta, f)

    _notify_ts_list(meta)


def get_tilt_grids(timestamp: str) -> tuple[dict[str, sp.csr_matrix], dict[str, Any]] | None:
    """Load all tilt grids for a timestamp. Returns (sparse_dict, metadata) or None."""
    stem = _ts_to_stem(timestamp)
    ts_dir = TILT_GRIDS_DIR / stem
    meta_path = ts_dir / "meta.json"

    if not meta_path.exists():
        return None

    with open(meta_path) as f:
        meta = json.load(f)

    grids: dict[str, sp.csr_matrix] = {}
    for npz_path in sorted(ts_dir.glob("*.npz")):
        if npz_path.stem == "motion":
            continue
        tilt = npz_path.stem  # e.g. "00.50"
        grids[tilt] = sp.load_npz(npz_path)

    if not grids:
        return None

    return grids, meta


def has_tilt_grids(timestamp: str) -> bool:
    """Check whether tilt grids exist on disk for *timestamp*."""
    stem = _ts_to_stem(timestamp)
    return (TILT_GRIDS_DIR / stem / "meta.json").exists()


def get_meta(timestamp: str) -> dict[str, Any] | None:
    """Load just the metadata (meta.json) for a timestamp, without grids."""
    stem = _ts_to_stem(timestamp)
    meta_path = TILT_GRIDS_DIR / stem / "meta.json"
    if not meta_path.exists():
        return None
    with open(meta_path) as f:
        return json.load(f)


def list_available_tilts(timestamp: str) -> list[str]:
    """Return the tilt level names that have .npz files on disk for *timestamp*."""
    stem = _ts_to_stem(timestamp)
    ts_dir = TILT_GRIDS_DIR / stem
    if not ts_dir.exists():
        return []
    return [p.stem for p in ts_dir.glob("*.npz") if p.stem != "motion"]


def get_single_tilt(timestamp: str, tilt: str) -> sp.csr_matrix | None:
    """Load a single tilt's sparse grid from disk."""
    stem = _ts_to_stem(timestamp)
    path = TILT_GRIDS_DIR / stem / f"{tilt}.npz"
    if not path.exists():
        return None
    return sp.load_npz(path)


# ── Motion field cache ────────────────────────────────────────────────────────


def put_motion(
    timestamp: str,
    u: np.ndarray,
    v: np.ndarray,
    confidence: np.ndarray,
    png_bytes: bytes,
) -> None:
    """Save motion field for *timestamp* (displacement toward next frame)."""
    stem = _ts_to_stem(timestamp)
    ts_dir = TILT_GRIDS_DIR / stem
    ts_dir.mkdir(parents=True, exist_ok=True)

    np.savez_compressed(
        ts_dir / "motion.npz", u=u, v=v, confidence=confidence,
    )
    (ts_dir / "motion.png").write_bytes(png_bytes)


def get_motion_png(timestamp: str) -> bytes | None:
    """Return pre-rendered motion PNG for *timestamp*, or None."""
    stem = _ts_to_stem(timestamp)
    path = TILT_GRIDS_DIR / stem / "motion.png"
    if path.exists():
        return path.read_bytes()
    return None


def has_motion(timestamp: str) -> bool:
    """Check whether a motion field exists on disk for *timestamp*."""
    stem = _ts_to_stem(timestamp)
    return (TILT_GRIDS_DIR / stem / "motion.png").exists()


_ts_list_cache: list[dict[str, Any]] | None = None
_ts_list_lock = threading.Lock()


def _meta_to_entry(meta: dict[str, Any]) -> dict[str, Any]:
    """Build a timestamps-list entry from a metadata dict."""
    ts = meta.get("timestamp")
    entry: dict[str, Any] = {
        "timestamp": ts,
        "bounds": {
            "north": meta.get("north"),
            "south": meta.get("south"),
            "east": meta.get("east"),
            "west": meta.get("west"),
        },
    }
    if ts:
        entry["has_motion"] = has_motion(ts)

    tilt_sources = meta.get("tilt_sources")
    if tilt_sources:
        entry["native_tilts"] = sum(
            1 for v in tilt_sources.values() if v.get("origin") == "native"
        )
        entry["total_tilts"] = sum(
            1 for v in tilt_sources.values() if v.get("origin") != "missing"
        )
    return entry


def _load_ts_list_from_disk() -> list[dict[str, Any]]:
    """One-time disk scan to bootstrap the timestamps list."""
    if not TILT_GRIDS_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for ts_dir in sorted(TILT_GRIDS_DIR.iterdir()):
        meta_path = ts_dir / "meta.json"
        if not meta_path.exists():
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        entries.append(_meta_to_entry(meta))
    return entries


def list_tilt_grid_timestamps() -> list[dict[str, Any]]:
    """Return metadata dicts for all tilt grid sets on disk, oldest-first.

    The list is loaded from disk once, then maintained incrementally
    by put_tilt_grids() as new frames are written.
    """
    global _ts_list_cache
    with _ts_list_lock:
        if _ts_list_cache is None:
            _ts_list_cache = _load_ts_list_from_disk()
        return _ts_list_cache


def _notify_ts_list(metadata: dict[str, Any]) -> None:
    """Insert or update an entry in the cached timestamps list after a disk write.

    Deduplicates by timestamp and maintains sorted order.
    """
    global _ts_list_cache
    entry = _meta_to_entry(metadata)
    ts = entry["timestamp"]
    with _ts_list_lock:
        if _ts_list_cache is None:
            _ts_list_cache = _load_ts_list_from_disk()
            return
        for i, e in enumerate(_ts_list_cache):
            if e["timestamp"] == ts:
                _ts_list_cache[i] = entry
                return
        _ts_list_cache.append(entry)
        _ts_list_cache.sort(key=lambda e: e["timestamp"] or "")


def invalidate_ts_list_cache() -> None:
    """Force the timestamps list to re-scan disk on next call."""
    global _ts_list_cache
    with _ts_list_lock:
        _ts_list_cache = None


# ── Eviction ──────────────────────────────────────────────────────────────────


def evict_timestamp(timestamp: str) -> bool:
    """Remove a single timestamp's tilt grid directory from disk."""
    stem = _ts_to_stem(timestamp)
    ts_dir = TILT_GRIDS_DIR / stem
    if ts_dir.exists():
        shutil.rmtree(ts_dir)
        return True
    return False


def evict_older_than(hours: float = 24.0) -> int:
    """Remove cached files older than `hours`."""
    cutoff = time.time() - hours * 3600
    removed = 0

    # Raw cache: walk tilt subdirectories
    if RAW_DIR.exists():
        for path in RAW_DIR.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1

    # Tilt grid cache: remove entire timestamp directories
    if TILT_GRIDS_DIR.exists():
        for ts_dir in list(TILT_GRIDS_DIR.iterdir()):
            if not ts_dir.is_dir():
                continue
            meta_path = ts_dir / "meta.json"
            check_path = meta_path if meta_path.exists() else ts_dir
            if check_path.stat().st_mtime < cutoff:
                shutil.rmtree(ts_dir)
                removed += 1

    if removed:
        logger.info("Evicted %d stale cache entries (>%.0fh old)", removed, hours)
    return removed


# ── Legacy migration ─────────────────────────────────────────────────────────


def migrate_legacy_cache() -> dict[str, int]:
    """Move files from old flat cache dirs into the per-tilt raw layout.

    Also removes stale legacy directories (decoded, composites).
    """
    stats = {"moved": 0, "deleted": 0, "errors": 0}

    # Migrate old flat raw cache
    if _LEGACY_RAW_DIR.exists():
        for path in list(_LEGACY_RAW_DIR.iterdir()):
            if not path.is_file():
                continue
            if "Composite" in path.name:
                path.unlink()
                stats["deleted"] += 1
                continue
            m = _TILT_RE.search(path.name)
            if not m:
                stats["errors"] += 1
                continue
            tilt = m.group(1)
            parts = path.name.split("_MRMS_", 1)
            short_name = "MRMS_" + parts[1] if len(parts) == 2 else path.name
            dest = RAW_DIR / tilt / short_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(dest))
                stats["moved"] += 1
            except OSError:
                logger.exception("Failed to move %s → %s", path, dest)
                stats["errors"] += 1
        try:
            if _LEGACY_RAW_DIR.exists() and not any(_LEGACY_RAW_DIR.iterdir()):
                _LEGACY_RAW_DIR.rmdir()
        except OSError:
            pass

    # Remove legacy decoded and composites directories
    for legacy in (_LEGACY_DECODED_DIR, _LEGACY_DECODED_DIR2, _LEGACY_COMPOSITES_DIR):
        if legacy.exists():
            shutil.rmtree(legacy, ignore_errors=True)
            logger.info("Removed legacy dir: %s", legacy)
            stats["deleted"] += 1

    if stats["moved"] or stats["deleted"]:
        logger.info(
            "Legacy migration: %d moved, %d deleted, %d errors",
            stats["moved"], stats["deleted"], stats["errors"],
        )
    return stats
