"""
MRMS data pipeline: fetch tilt-level reflectivity, store as sparse grids.

Single fetch path: tilt-level MergedReflectivityQC files from S3.
Output: sparse CSR tilt grids on disk + in-memory LRU cache.

Both 2D composite tiles and 3D voxel tiles are derived on-demand at
tile-serve time — no pre-computation of composites or voxels.
"""

from __future__ import annotations

import gzip
import logging
import re

import numpy as np
import scipy.sparse as sp

from . import disk_cache
from .cache import tilt_cache
from .grib2.decoder import decode_grib2
from .mrms import S3KeyNotFound, fetch_raw, list_latest_files, mask_sentinel_values

logger = logging.getLogger(__name__)

TILT_LEVELS = ["00.50", "01.50", "02.50", "03.50", "05.00", "07.00", "10.00", "14.00"]

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
    """Parse an ISO timestamp from an MRMS S3 key filename."""
    m = _TS_RE.search(s3_key)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}Z"


# ── Per-tilt fetch + decode ───────────────────────────────────────────────────


def _decode_tilt_full(s3_key: str) -> tuple[np.ndarray, dict] | None:
    """Fetch + decode one tilt to the full CONUS grid. Returns (grid, metadata) or None."""
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


# ── Per-timestep frame builder ────────────────────────────────────────────────


def _build_frame(ref_key: str, pool=None) -> tuple[str, dict[str, sp.csr_matrix], dict] | None:
    """Fetch all tilts, convert to sparse, return (timestamp, sparse_grids, metadata).

    If tilt grids already exist on disk for this timestamp, skips entirely.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    timestamp = _timestamp_from_key(ref_key)
    if timestamp is None:
        return None

    if disk_cache.has_tilt_grids(timestamp):
        result = disk_cache.get_tilt_grids(timestamp)
        if result is not None:
            grids, meta = result
            return timestamp, grids, meta

    tilt_keys = [(tilt, derive_tilt_key(ref_key, tilt)) for tilt in TILT_LEVELS]
    decoded: dict[str, tuple[np.ndarray, dict]] = {}

    def _do_tilt(tilt_key_pair):
        tilt, key = tilt_key_pair
        return tilt, _decode_tilt_full(key)

    executor = pool or ThreadPoolExecutor(max_workers=8)
    own_pool = pool is None
    try:
        futures = {executor.submit(_do_tilt, tk): tk for tk in tilt_keys}
        for f in as_completed(futures):
            tilt, result = f.result()
            if result is not None:
                decoded[tilt] = result
    finally:
        if own_pool:
            executor.shutdown(wait=False)

    if not decoded:
        return None

    meta = next(iter(decoded.values()))[1]

    sparse_grids: dict[str, sp.csr_matrix] = {}
    for tilt, (grid, _) in decoded.items():
        g = grid.copy()
        g[np.isnan(g)] = 0
        sparse_grids[tilt] = sp.csr_matrix(g)

    disk_cache.put_tilt_grids(timestamp, sparse_grids, meta)
    return timestamp, sparse_grids, meta


# ── Public API ────────────────────────────────────────────────────────────────


def seed_frames(count: int = 60) -> int:
    """Pre-fetch and cache tilt grids for the most recent timestamps.

    Returns number of timestamps now cached.
    """
    from concurrent.futures import ThreadPoolExecutor

    logger.info("Seeding frame cache (%d frames across %d tilts)…", count, len(TILT_LEVELS))
    ref_keys = list_tilt_files("00.50", count=count)

    if not ref_keys:
        logger.warning("No 00.50 tilt files found — seeding skipped")
        return 0

    ordered = list(reversed(ref_keys))  # oldest-first

    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, ref_key in enumerate(ordered):
            result = _build_frame(ref_key, pool=pool)
            if result is None:
                logger.warning("Could not build frame for %s", ref_key)
                continue
            timestamp, sparse_grids, meta = result
            tilt_cache.put(timestamp, sparse_grids, meta)
            n_nnz = sum(s.nnz for s in sparse_grids.values())
            logger.info("  [%d/%d] %s: %d tilts, %d active cells",
                        i + 1, len(ordered), timestamp, len(sparse_grids), n_nnz)

    total = tilt_cache.count()
    logger.info("Frame cache seeded: %d timestamps in memory, %d on disk",
                total, len(disk_cache.list_tilt_grid_timestamps()))
    return total


def invalidate_all() -> None:
    """Clear all in-memory caches."""
    tilt_cache.clear()
    from .tiles import bin_tile_cache, tile_cache, voxel_tile_cache
    tile_cache.clear()
    voxel_tile_cache.clear()
    bin_tile_cache.clear()
    disk_cache.invalidate_ts_list_cache()
