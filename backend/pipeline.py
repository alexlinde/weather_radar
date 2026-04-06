"""
Unified MRMS data pipeline: fetch tilt-level reflectivity, produce both
CONUS-wide 2D composites (for tile serving) and NYC-region 3D voxel volumes.

Single fetch path: tilt-level MergedReflectivityQC files from S3.
Three output stores per frame:
  - CONUS composite grids → disk (data/composites/) + in-memory LRU
  - NYC composite grids   → cache.composite_cache (legacy endpoints)
  - NYC 3D voxels         → cache.volume_cache
"""

from __future__ import annotations

import gzip
import logging
import re
from typing import Any

import numpy as np

from . import disk_cache
from .cache import FrameEntry, composite_cache, volume_cache
from .grib2.decoder import decode_grib2
from .mrms import NYC_BBOX, S3KeyNotFound, clip_to_bbox, fetch_raw, list_latest_files, mask_sentinel_values

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

_TS_RE = re.compile(r"_(\d{8})-(\d{6})\.grib2")


# ── S3 key helpers ────────────────────────────────────────────────────────────


def list_tilt_files(tilt: str, count: int = 60) -> list[str]:
    """List the most recent GRIB2 keys for a given tilt level, newest-first."""
    product = VOLUME_PRODUCT_TEMPLATE.format(tilt=tilt)
    return list_latest_files(product, count=count)


def derive_tilt_key(ref_key: str, tilt: str) -> str:
    """Derive the S3 key for a different tilt level from a reference 00.50 key."""
    ref_tilt = ref_key.split("/")[1].split("_")[-1]
    return ref_key.replace(f"MergedReflectivityQC_{ref_tilt}", f"MergedReflectivityQC_{tilt}")


def _timestamp_from_key(s3_key: str) -> str | None:
    """Parse an ISO timestamp from an MRMS S3 key filename.

    ``MRMS_..._20260405-120000.grib2.gz`` → ``2026-04-05T12:00:00Z``
    """
    m = _TS_RE.search(s3_key)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}Z"


# ── Per-tilt fetch + decode ───────────────────────────────────────────────────


def _fetch_and_decode_tilt(s3_key: str, bbox: dict | None = None) -> tuple[np.ndarray, dict] | None:
    """Fetch + decode one tilt file (NYC-clipped). Returns (clipped_grid, metadata) or None."""
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
    except S3KeyNotFound:
        logger.warning("Not yet available in S3: %s", s3_key)
        return None
    except Exception:
        logger.exception("Failed to decode %s", s3_key)
        return None


def _decode_tilt_full(s3_key: str) -> tuple[np.ndarray, dict] | None:
    """Fetch + decode one tilt to the full CONUS grid (no clip). Returns (grid, metadata) or None."""
    try:
        raw_gz = fetch_raw(s3_key)
        raw = gzip.decompress(raw_gz)
        metadata, grid = decode_grib2(raw)
        grid = mask_sentinel_values(grid)
        return grid, metadata
    except S3KeyNotFound:
        logger.warning("Not yet available in S3: %s", s3_key)
        return None
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

        all_lons.append(west + Di / 2 + cols * Di)
        all_lats.append(north - Dj / 2 - rows * Dj)
        all_alts.append(np.full(len(rows), height_m))
        all_dbz.append(grid[rows, cols])

    if not all_lons:
        return []

    lons = np.round(np.concatenate(all_lons), 5)
    lats = np.round(np.concatenate(all_lats), 5)
    alts = np.concatenate(all_alts)
    dbz = np.round(np.concatenate(all_dbz), 1)

    return np.column_stack([lons, lats, alts, dbz]).tolist()


# ── Per-timestep frame builders ───────────────────────────────────────────────


def _build_volume_frame(
    ref_key: str, bbox: dict, pool=None
) -> tuple[str, list, np.ndarray, dict] | None:
    """Fetch all tilts, produce CONUS composite + NYC voxels in one decode pass.

    Full CONUS grids are decoded for the composite (saved to disk).
    NYC-clipped grids are derived for voxels + legacy composite cache.

    Returns (timestamp, voxels, nyc_composite_grid, nyc_metadata) or None.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tilt_keys = [(tilt, derive_tilt_key(ref_key, tilt)) for tilt in TILT_LEVELS]

    ts_hint = _timestamp_from_key(ref_key)
    have_conus = ts_hint is not None and disk_cache.has_composite(ts_hint)

    conus_grids: list[tuple[str, np.ndarray]] = []
    nyc_grids: list[tuple[str, np.ndarray]] = []
    conus_meta: dict | None = None
    nyc_meta: dict | None = None
    timestamp: str | None = None

    def _do_tilt(tilt_key_pair):
        tilt, key = tilt_key_pair

        if have_conus:
            nyc_result = _fetch_and_decode_tilt(key, bbox)
            return tilt, nyc_result, None

        full_result = _decode_tilt_full(key)
        if full_result is None:
            return tilt, None, None

        full_grid, full_meta = full_result
        clipped, clipped_meta = clip_to_bbox(full_grid, full_meta, bbox)
        disk_cache.put_decoded(key, clipped, clipped_meta)
        return tilt, (clipped, clipped_meta), (full_grid, full_meta)

    executor = pool or ThreadPoolExecutor(max_workers=8)
    own_pool = pool is None
    try:
        futures = {executor.submit(_do_tilt, tk): tk for tk in tilt_keys}
        for f in as_completed(futures):
            tilt, nyc_result, conus_result = f.result()

            if nyc_result is not None:
                clipped, clipped_meta = nyc_result
                nyc_grids.append((tilt, clipped))
                if nyc_meta is None:
                    nyc_meta = clipped_meta
                    timestamp = clipped_meta.get("timestamp")

            if conus_result is not None:
                full_grid, full_meta = conus_result
                conus_grids.append((tilt, full_grid))
                if conus_meta is None:
                    conus_meta = full_meta
    finally:
        if own_pool:
            executor.shutdown(wait=False)

    if not nyc_grids or nyc_meta is None:
        return None

    if conus_grids and conus_meta is not None and timestamp:
        conus_composite = composite_from_grids(conus_grids)
        disk_cache.put_composite(timestamp, conus_composite, conus_meta)
    del conus_grids

    nyc_composite = composite_from_grids(nyc_grids)
    voxels = _compute_volume_voxels(nyc_grids, nyc_meta)
    return timestamp, voxels, nyc_composite, nyc_meta


# ── Public API ────────────────────────────────────────────────────────────────


def seed_frames(count: int = 60, bbox: dict | None = None) -> int:
    """Pre-fetch and cache data for the most recent volume snapshots.

    Produces:
      - CONUS composites saved to disk (for tile serving)
      - NYC composites in composite_cache (legacy endpoints)
      - NYC voxels in volume_cache (3D mode)

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
    logger.info("Frame cache seeded: %d volume / %d composite / %d CONUS on disk",
                total, composite_cache.count(), len(disk_cache.list_composites()))
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
    """Clear all in-memory caches."""
    composite_cache.clear()
    volume_cache.clear()
    from .tiles import tile_cache
    tile_cache.clear()
