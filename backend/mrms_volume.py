"""
Multi-level MRMS reflectivity volume: fetch, decode, and assemble into 3D columns.

Uses the MergedReflectivityQC product at multiple tilt angles to build a
vertical profile of radar reflectivity over the NYC region.

Fetch pipeline mirrors mrms.py: disk cache -> S3.

Frame cache: stores pre-computed column data for up to MAX_VOLUME_FRAMES
time steps, oldest-first, keyed by ISO timestamp string.
"""

from __future__ import annotations

import gzip
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from . import disk_cache
from .grib2.decoder import decode_grib2
from .mrms import BUCKET, NYC_BBOX, clip_to_bbox, fetch_raw, mask_sentinel_values

logger = logging.getLogger(__name__)

# The 8 tilt levels we sample, chosen for good vertical coverage
TILT_LEVELS = ["00.50", "01.50", "02.50", "03.50", "05.00", "07.00", "10.00", "14.00"]

# Representative altitude (km) for each tilt at typical ranges for the NYC region
TILT_TO_HEIGHT_KM: dict[str, float] = {
    "00.50": 1.0,
    "01.50": 2.0,
    "02.50": 3.5,
    "03.50": 5.0,
    "05.00": 7.0,
    "07.00": 9.0,
    "10.00": 12.0,
    "14.00": 15.0,
}

VOLUME_PRODUCT_TEMPLATE = "CONUS/MergedReflectivityQC_{tilt}"

# Multi-frame volume cache: timestamp → pre-computed columns list
# OrderedDict preserves chronological insertion order (oldest first)
_volume_frames: OrderedDict[str, list] = OrderedDict()
MAX_VOLUME_FRAMES = 70
_lock = threading.Lock()


# ── S3 helpers ────────────────────────────────────────────────────────────────


def list_tilt_files(tilt: str, count: int = 60) -> list[str]:
    """List the most recent GRIB2 keys for a given tilt level, newest-first.
    Spans across UTC day boundaries to collect enough files."""
    from .mrms import _s3_client

    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    product = VOLUME_PRODUCT_TEMPLATE.format(tilt=tilt)
    now = datetime.now(timezone.utc)

    all_keys: list[str] = []
    for delta in range(3):
        day = now - timedelta(days=delta)
        date_str = day.strftime("%Y%m%d")
        prefix = f"{product}/{date_str}/"

        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".grib2.gz"):
                    all_keys.append(key)

        if len(all_keys) >= count:
            break

    all_keys.sort(reverse=True)
    return all_keys[:count]


def derive_tilt_key(ref_key: str, tilt: str) -> str:
    """
    Derive the S3 key for a different tilt level from a reference 00.50 key.

    Example:
      ref_key = 'CONUS/MergedReflectivityQC_00.50/20260405/MRMS_..._00.50_20260405-230640.grib2.gz'
      tilt    = '01.50'
      result  = 'CONUS/MergedReflectivityQC_01.50/20260405/MRMS_..._01.50_20260405-230640.grib2.gz'
    """
    ref_tilt = ref_key.split("/")[1].split("_")[-1]
    return ref_key.replace(f"MergedReflectivityQC_{ref_tilt}", f"MergedReflectivityQC_{tilt}")


# ── Per-frame volume computation ──────────────────────────────────────────────


def _fetch_and_decode_tilt(s3_key: str, bbox: dict | None = None) -> tuple[np.ndarray, dict] | None:
    """Fetch + decode one tilt file. Returns (clipped_grid, metadata) or None on error."""
    bbox = bbox or NYC_BBOX
    try:
        decoded = disk_cache.get_decoded(s3_key)
        if decoded is not None:
            return decoded

        raw_gz = fetch_raw(s3_key)
        raw = gzip.decompress(raw_gz)
        metadata, grid = decode_grib2(raw)
        grid = mask_sentinel_values(grid)
        clipped, clipped_meta = clip_to_bbox(grid, metadata, bbox)
        disk_cache.put_decoded(s3_key, clipped, clipped_meta)
        return clipped, clipped_meta
    except Exception:
        logger.exception("Failed to decode %s", s3_key)
        return None


def _compute_volume_voxels(
    grids: list[tuple[str, np.ndarray]],
    meta: dict,
    min_dbz: float = 10.0,
) -> list[list[float]]:
    """
    Emit one voxel per active (cell, level) for deck.gl PointCloudLayer.

    Returns list of [lon, lat, altitude_m, dbz].
    Only includes voxels with dbz >= min_dbz.
    """
    if not grids:
        return []

    north = meta["north"]
    west = meta["west"]
    Dj = meta["Dj"]
    Di = meta["Di"]

    all_lons = []
    all_lats = []
    all_alts = []
    all_dbz = []

    for tilt_str, grid in grids:
        height_m = TILT_TO_HEIGHT_KM[tilt_str] * 1000.0
        mask = (~np.isnan(grid)) & (grid >= min_dbz)
        rows, cols = np.where(mask)
        if len(rows) == 0:
            continue

        all_lons.append(west + cols * Di)
        all_lats.append(north - rows * Dj)
        all_alts.append(np.full(len(rows), height_m))
        all_dbz.append(grid[rows, cols])

    if not all_lons:
        return []

    lons = np.round(np.concatenate(all_lons), 5)
    lats = np.round(np.concatenate(all_lats), 5)
    alts = np.concatenate(all_alts)
    dbz = np.round(np.concatenate(all_dbz), 1)

    return np.column_stack([lons, lats, alts, dbz]).tolist()


def _build_volume_frame(ref_key: str, bbox: dict, pool=None) -> tuple[str, list] | None:
    """
    Fetch all tilt levels for the timestamp encoded in ref_key.
    If a ThreadPoolExecutor is provided, fetches tilts in parallel.
    Returns (timestamp_iso, columns) or None if all tilts fail.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tilt_keys = [(tilt, derive_tilt_key(ref_key, tilt)) for tilt in TILT_LEVELS]

    grids: list[tuple[str, np.ndarray]] = []
    meta = None
    timestamp = None

    def _do_tilt(tilt_key_pair):
        tilt, key = tilt_key_pair
        result = _fetch_and_decode_tilt(key, bbox)
        return tilt, result

    executor = pool or ThreadPoolExecutor(max_workers=8)
    own_pool = pool is None
    try:
        futures = {executor.submit(_do_tilt, tk): tk for tk in tilt_keys}
        for f in as_completed(futures):
            tilt, result = f.result()
            if result is None:
                continue
            grid, m = result
            grids.append((tilt, grid))
            if meta is None:
                meta = m
                timestamp = m.get("timestamp")
    finally:
        if own_pool:
            executor.shutdown(wait=False)

    if not grids or meta is None:
        return None

    voxels = _compute_volume_voxels(grids, meta)
    return timestamp, voxels


# ── Frame cache API ───────────────────────────────────────────────────────────


def _store_frame(timestamp: str, columns: list) -> None:
    with _lock:
        if timestamp in _volume_frames:
            return
        if len(_volume_frames) >= MAX_VOLUME_FRAMES:
            _volume_frames.popitem(last=False)  # evict oldest
        _volume_frames[timestamp] = columns


def get_volume_frames(count: int = 60) -> list[dict]:
    """Return up to `count` cached volume frames, oldest-first."""
    with _lock:
        items = list(_volume_frames.items())
    recent = items[-count:] if count < len(items) else items
    return [{"timestamp": ts, "voxels": cols} for ts, cols in recent]


def volume_frame_count() -> int:
    with _lock:
        return len(_volume_frames)


def seed_volume_frames(count: int = 60, bbox: dict | None = None) -> int:
    """
    Pre-fetch and cache column data for the `count` most recent volume snapshots.
    Uses a shared ThreadPoolExecutor so tilt-level decodes run in parallel.
    Returns number of frames now cached.
    """
    from concurrent.futures import ThreadPoolExecutor

    bbox = bbox or NYC_BBOX
    logger.info("Seeding volume frame cache (%d frames across %d tilts)…", count, len(TILT_LEVELS))
    ref_keys = list_tilt_files("00.50", count=count)

    if not ref_keys:
        logger.warning("No 00.50 tilt files found — volume seeding skipped")
        return 0

    ordered = list(reversed(ref_keys))  # oldest-first

    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, ref_key in enumerate(ordered):
            result = _build_volume_frame(ref_key, bbox, pool=pool)
            if result is None:
                logger.warning("Could not build volume frame for %s", ref_key)
                continue
            timestamp, columns = result
            _store_frame(timestamp, columns)
            logger.info("  [%d/%d] volume frame %s: %d voxels",
                        i + 1, len(ordered), timestamp, len(columns))

    total = volume_frame_count()
    logger.info("Volume frame cache seeded: %d frames", total)
    return total


def invalidate_volume_cache() -> None:
    with _lock:
        _volume_frames.clear()


# ── Legacy single-snapshot API (kept for /api/radar/volume backward compat) ───


def fetch_volume_snapshot(bbox: dict | None = None) -> dict[str, Any]:
    """Return the latest volume frame as a full snapshot dict."""
    bbox = bbox or NYC_BBOX
    with _lock:
        if _volume_frames:
            ts, cols = next(reversed(_volume_frames.items()))
            return {
                "timestamp": ts,
                "bounds": {k: v for k, v in bbox.items()},
                "voxels": cols,
            }

    # No cached frames — fetch live
    ref_keys = list_tilt_files("00.50", count=1)
    if not ref_keys:
        raise RuntimeError("No tilt files found in S3")

    result = _build_volume_frame(ref_keys[0], bbox)
    if result is None:
        raise RuntimeError("All tilt levels failed")

    timestamp, columns = result
    _store_frame(timestamp, columns)
    return {
        "timestamp": timestamp,
        "bounds": {k: v for k, v in bbox.items()},
        "voxels": columns,
    }
