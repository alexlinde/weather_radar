"""
Unified MRMS data pipeline: fetch tilt-level reflectivity, produce both
2D composites (derived via nanmax across tilts) and 3D voxel volumes.

Single fetch path: tilt-level MergedReflectivityQC files from S3.
Two cache stores per frame:
  - 2D composite grids → cache.composite_cache
  - 3D voxels → cache.volume_cache
"""

from __future__ import annotations

import gzip
import logging
from typing import Any

import numpy as np

from . import disk_cache
from .cache import FrameEntry, composite_cache, volume_cache
from .grib2.decoder import decode_grib2
from .mrms import NYC_BBOX, clip_to_bbox, fetch_raw, list_latest_files, mask_sentinel_values

logger = logging.getLogger(__name__)

TILT_LEVELS = ["00.50", "01.50", "02.50", "03.50", "05.00", "07.00", "10.00", "14.00"]

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


# ── S3 key helpers ────────────────────────────────────────────────────────────


def list_tilt_files(tilt: str, count: int = 60) -> list[str]:
    """List the most recent GRIB2 keys for a given tilt level, newest-first."""
    product = VOLUME_PRODUCT_TEMPLATE.format(tilt=tilt)
    return list_latest_files(product, count=count)


def derive_tilt_key(ref_key: str, tilt: str) -> str:
    """Derive the S3 key for a different tilt level from a reference 00.50 key."""
    ref_tilt = ref_key.split("/")[1].split("_")[-1]
    return ref_key.replace(f"MergedReflectivityQC_{ref_tilt}", f"MergedReflectivityQC_{tilt}")


# ── Per-tilt fetch + decode ───────────────────────────────────────────────────


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


# ── Composite + voxel derivation ──────────────────────────────────────────────


def composite_from_grids(grids: list[tuple[str, np.ndarray]]) -> np.ndarray:
    """Max reflectivity across all tilt levels at each grid point."""
    if not grids:
        return np.array([])
    ref_shape = grids[0][1].shape
    same_shape = [g for _, g in grids if g.shape == ref_shape]
    return np.nanmax(np.stack(same_shape), axis=0)


def _compute_volume_voxels(
    grids: list[tuple[str, np.ndarray]],
    meta: dict,
    min_dbz: float = 10.0,
) -> list[list[float]]:
    """Emit one voxel per active (cell, level) for deck.gl PointCloudLayer.

    Returns list of [lon, lat, altitude_m, dbz].
    """
    if not grids:
        return []

    north = meta["north"]
    west = meta["west"]
    Dj = meta["Dj"]
    Di = meta["Di"]

    all_lons: list[np.ndarray] = []
    all_lats: list[np.ndarray] = []
    all_alts: list[np.ndarray] = []
    all_dbz: list[np.ndarray] = []

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


# ── Per-timestep frame builder ────────────────────────────────────────────────


def _build_volume_frame(
    ref_key: str, bbox: dict, pool=None
) -> tuple[str, list, np.ndarray, dict] | None:
    """Fetch all tilt levels for the timestamp in ref_key.

    Returns (timestamp, voxels, composite_grid, metadata) or None if all tilts fail.
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
    composite = composite_from_grids(grids)
    return timestamp, voxels, composite, meta


# ── Public API ────────────────────────────────────────────────────────────────


def seed_frames(count: int = 60, bbox: dict | None = None) -> int:
    """Pre-fetch and cache data for the most recent volume snapshots.

    Stores both 3D voxels (volume_cache) and derived 2D composites
    (composite_cache) from a single set of tilt fetches.
    Returns number of volume frames now cached.
    """
    from concurrent.futures import ThreadPoolExecutor

    bbox = bbox or NYC_BBOX
    logger.info("Seeding frame cache (%d frames across %d tilts)…", count, len(TILT_LEVELS))
    ref_keys = list_tilt_files("00.50", count=count)

    if not ref_keys:
        logger.warning("No 00.50 tilt files found — seeding skipped")
        return 0

    ordered = list(reversed(ref_keys))  # oldest-first

    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, ref_key in enumerate(ordered):
            result = _build_volume_frame(ref_key, bbox, pool=pool)
            if result is None:
                logger.warning("Could not build volume frame for %s", ref_key)
                continue
            timestamp, voxels, comp_grid, meta = result
            volume_cache.put(timestamp, voxels)
            composite_cache.put(ref_key, FrameEntry(metadata=meta, grid=comp_grid))
            logger.info("  [%d/%d] %s: %d voxels, composite %s",
                        i + 1, len(ordered), timestamp, len(voxels),
                        comp_grid.shape)

    total = volume_cache.count()
    logger.info("Frame cache seeded: %d volume / %d composite", total, composite_cache.count())
    return total


def fetch_latest_composite(bbox: dict | None = None) -> tuple[np.ndarray, dict]:
    """Fetch one volume snapshot and return the derived 2D composite.

    Cold-start fallback when composite_cache is empty.
    """
    bbox = bbox or NYC_BBOX
    ref_keys = list_tilt_files("00.50", count=1)
    if not ref_keys:
        raise RuntimeError("No tilt files found in S3")

    result = _build_volume_frame(ref_keys[0], bbox)
    if result is None:
        raise RuntimeError("All tilt levels failed")

    timestamp, voxels, comp_grid, meta = result
    volume_cache.put(timestamp, voxels)
    composite_cache.put(ref_keys[0], FrameEntry(metadata=meta, grid=comp_grid))
    return comp_grid, meta


def fetch_volume_snapshot(bbox: dict | None = None) -> dict[str, Any]:
    """Return the latest volume frame as a full snapshot dict."""
    bbox = bbox or NYC_BBOX

    entry = volume_cache.latest()
    if entry is not None:
        ts, voxels = entry
        return {
            "timestamp": ts,
            "bounds": dict(bbox),
            "voxels": voxels,
        }

    # No cached frames — fetch live
    ref_keys = list_tilt_files("00.50", count=1)
    if not ref_keys:
        raise RuntimeError("No tilt files found in S3")

    result = _build_volume_frame(ref_keys[0], bbox)
    if result is None:
        raise RuntimeError("All tilt levels failed")

    timestamp, voxels, comp_grid, meta = result
    volume_cache.put(timestamp, voxels)
    composite_cache.put(ref_keys[0], FrameEntry(metadata=meta, grid=comp_grid))
    return {
        "timestamp": timestamp,
        "bounds": dict(bbox),
        "voxels": voxels,
    }


def get_volume_frames(count: int = 60) -> list[dict]:
    """Return up to `count` cached volume frames, oldest-first."""
    return [{"timestamp": ts, "voxels": v} for ts, v in volume_cache.items(count)]


def invalidate_all() -> None:
    """Clear both in-memory caches."""
    composite_cache.clear()
    volume_cache.clear()
