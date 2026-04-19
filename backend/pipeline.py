"""
MRMS data pipeline: fetch tilt-level reflectivity, store as sparse grids.

Single fetch path: tilt-level MergedReflectivityQC files from S3.
Output: sparse CSR tilt grids on disk + in-memory LRU cache.

Binary voxel tiles for the default CONUS viewport (z=4) are pre-rendered
after seeding/warming so the bulk endpoint serves instantly on first request.
"""

from __future__ import annotations

import gzip
import logging
import re
import time

import numpy as np
import scipy.sparse as sp

from . import disk_cache
from .cache import tilt_cache
from .grib2.decoder import decode_grib2
from .mrms import S3KeyNotFound, fetch_raw, list_latest_files, mask_sentinel_values

logger = logging.getLogger(__name__)

TILT_LEVELS = ["00.50", "01.00", "01.50", "02.50", "04.00", "07.00", "10.00", "19.00"]

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


def _decode_tilt_full(s3_key: str, retries: int = 1) -> tuple[np.ndarray, dict] | None:
    """Fetch + decode one tilt to the full CONUS grid. Returns (grid, metadata) or None.

    Retries once on S3KeyNotFound to handle files still propagating in S3.
    """
    for attempt in range(1 + retries):
        try:
            raw_gz = fetch_raw(s3_key)
            raw = gzip.decompress(raw_gz)
            metadata, grid = decode_grib2(raw)
            grid = mask_sentinel_values(grid)
            return grid, metadata
        except S3KeyNotFound:
            if attempt < retries:
                time.sleep(3)
                continue
            logger.warning("Not yet available in S3: %s", s3_key)
            return None
        except Exception:
            logger.exception("Failed to decode %s", s3_key)
            return None
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
        sparse_grids[tilt] = sp.csr_matrix(np.nan_to_num(grid, nan=0.0))

    disk_cache.put_tilt_grids(timestamp, sparse_grids, meta)
    return timestamp, sparse_grids, meta


# ── Public API ────────────────────────────────────────────────────────────────


def _conus_tile_coords(z: int = 4) -> list[tuple[int, int, int]]:
    """Return (z, x, y) tile coords covering CONUS at the given zoom level."""
    import math
    n = 2 ** z
    lon_min, lon_max = -130.0, -60.0
    lat_min, lat_max = 22.0, 52.0
    x_min = max(0, int((lon_min + 180) / 360 * n))
    x_max = min(n - 1, int((lon_max + 180) / 360 * n))
    rad_max = math.radians(lat_max)
    rad_min = math.radians(lat_min)
    y_min = max(0, int((1 - math.log(math.tan(rad_max) + 1 / math.cos(rad_max)) / math.pi) / 2 * n))
    y_max = min(n - 1, int((1 - math.log(math.tan(rad_min) + 1 / math.cos(rad_min)) / math.pi) / 2 * n))
    return [(z, x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]


def _rebuild_from_raw(limit: int = 30) -> int:
    """Rebuild tilt grids from raw GRIB2 files when tilt_grids/ is missing.

    Scans data/raw/ for available timestamps, decodes, and stores as
    sparse grids — no S3 access required.
    """
    from concurrent.futures import ThreadPoolExecutor

    raw_dir = disk_cache.RAW_DIR
    if not raw_dir.exists():
        return 0

    ref_tilt_dir = raw_dir / "00.50"
    if not ref_tilt_dir.exists():
        return 0

    raw_files = sorted(ref_tilt_dir.glob("MRMS_*.grib2.gz"))
    if not raw_files:
        return 0

    raw_files = raw_files[-limit:]
    logger.info("Rebuilding %d frames from raw GRIB2 files…", len(raw_files))

    rebuilt = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for raw_path in raw_files:
            m = _TS_RE.search(raw_path.name)
            if not m:
                continue
            d, t = m.group(1), m.group(2)
            timestamp = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}Z"

            if disk_cache.has_tilt_grids(timestamp):
                result = disk_cache.get_tilt_grids(timestamp)
                if result:
                    grids, meta = result
                    tilt_cache.put(timestamp, grids, meta)
                    rebuilt += 1
                    continue

            ref_key = f"CONUS/MergedReflectivityQC_00.50/{d}/{raw_path.name}"
            result = _build_frame(ref_key, pool=pool)
            if result is None:
                continue
            ts, sparse_grids, meta = result
            tilt_cache.put(ts, sparse_grids, meta)
            rebuilt += 1
            logger.info("  Rebuilt %s: %d tilts", ts, len(sparse_grids))

    return rebuilt


def _prerender_atlas_tiles(entries: list[dict], tile_coords: list[tuple[int, int, int]]) -> int:
    """Pre-render atlas PNG tiles for the given timestamps and tile coordinates."""
    from .tiles import atlas_tile_cache, render_atlas_tile

    count = 0
    for entry in entries:
        ts = entry["timestamp"]
        tilt_entry = tilt_cache.get(ts)
        if tilt_entry is None:
            continue
        grids, meta = tilt_entry["grids"], tilt_entry["meta"]
        for z, x, y in tile_coords:
            cache_key = (ts, z, x, y)
            if atlas_tile_cache.get(cache_key) is None:
                data = render_atlas_tile(grids, meta, z, x, y)
                atlas_tile_cache.put(cache_key, data)
                count += 1
    return count


def warm_from_disk(limit: int = 30) -> int:
    """Load the most recent frames from disk into the in-memory cache.

    Skips all S3 fetching — useful for dev mode when you already have
    cached data and don't want to wait for a full re-seed.
    Pre-renders atlas tiles for CONUS z=4.

    Falls back to rebuilding from raw GRIB2 files if tilt_grids/ is
    empty (e.g. after a cache wipe).
    """
    entries = disk_cache.list_tilt_grid_timestamps()
    if not entries:
        logger.info("No tilt grids on disk — attempting rebuild from raw files")
        rebuilt = _rebuild_from_raw(limit=limit)
        if rebuilt == 0:
            logger.warning("No raw files found either — nothing to warm")
            return 0
        entries = disk_cache.list_tilt_grid_timestamps()

    recent = entries[-limit:]
    loaded = 0
    for entry in recent:
        ts = entry["timestamp"]
        if tilt_cache.get(ts) is not None:
            loaded += 1
            continue
        result = disk_cache.get_tilt_grids(ts)
        if result is None:
            continue
        grids, meta = result
        tilt_cache.put(ts, grids, meta)
        loaded += 1

    logger.info("Warmed %d/%d frames from disk into memory", loaded, len(recent))

    tile_coords_z4 = _conus_tile_coords(z=4)
    atlas_count = _prerender_atlas_tiles(recent, tile_coords_z4)
    tile_coords_z5 = _conus_tile_coords(z=5)
    recent_z5 = recent[-10:]
    atlas_count += _prerender_atlas_tiles(recent_z5, tile_coords_z5)
    logger.info("Pre-rendered %d atlas tiles (z=4 full + z=5 recent)", atlas_count)

    compute_all_motion()
    return loaded


def _log_gap_stats() -> None:
    """Log frame gap statistics for diagnostics."""
    from datetime import datetime

    entries = disk_cache.list_tilt_grid_timestamps()
    timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]
    if len(timestamps) < 2:
        return
    deltas = []
    for i in range(len(timestamps) - 1):
        t0 = datetime.fromisoformat(timestamps[i].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(timestamps[i + 1].replace("Z", "+00:00"))
        deltas.append((t1 - t0).total_seconds())
    median = sorted(deltas)[len(deltas) // 2]
    large_gaps = [d for d in deltas if d > median * 1.5]
    logger.info(
        "Frame gaps: %d/%d transitions >%.0fs (median=%.0fs, max=%.0fs)",
        len(large_gaps), len(deltas), median * 1.5, median, max(deltas),
    )


def seed_frames(count: int = 60) -> int:
    """Pre-fetch and cache tilt grids for the most recent timestamps.

    After fetching, pre-renders atlas and legacy voxel tiles for the
    default CONUS viewport (z=4).  Returns number of timestamps cached.
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

    _log_gap_stats()

    tile_coords_z4 = _conus_tile_coords(z=4)
    entries = disk_cache.list_tilt_grid_timestamps()
    recent = entries[-30:]
    atlas_count = _prerender_atlas_tiles(recent, tile_coords_z4)
    tile_coords_z5 = _conus_tile_coords(z=5)
    recent_z5 = recent[-10:]
    atlas_count += _prerender_atlas_tiles(recent_z5, tile_coords_z5)
    logger.info("Pre-rendered %d atlas tiles for default viewport (z=4 + z=5)", atlas_count)

    compute_all_motion()
    return total


def compute_all_motion() -> int:
    """Compute motion fields for all consecutive frame pairs.

    Skips pairs that already have motion data on disk.
    Returns the number of pairs computed.
    """
    import time as _time
    from .motion import compute_composite, compute_motion_field, encode_motion_png

    entries = disk_cache.list_tilt_grid_timestamps()
    if len(entries) < 2:
        return 0

    computed = 0
    t0 = _time.monotonic()

    for i in range(len(entries) - 1):
        ts_a = entries[i]["timestamp"]
        ts_b = entries[i + 1]["timestamp"]
        if not ts_a or not ts_b:
            continue
        if disk_cache.has_motion(ts_a):
            continue

        entry_a = tilt_cache.get(ts_a)
        entry_b = tilt_cache.get(ts_b)
        if entry_a is None or entry_b is None:
            continue

        try:
            comp_a = compute_composite(entry_a["grids"])
            comp_b = compute_composite(entry_b["grids"])
            u, v, conf = compute_motion_field(comp_a, comp_b)
            png = encode_motion_png(u, v, conf)
            disk_cache.put_motion(ts_a, u, v, conf, png)
            computed += 1
        except Exception:
            logger.exception("Motion computation failed for %s → %s", ts_a, ts_b)

    elapsed = _time.monotonic() - t0
    if computed:
        logger.info("Computed %d motion fields in %.1fs (%.2fs/pair)",
                     computed, elapsed, elapsed / computed)
    return computed


def refresh_new_frames(count: int = 10) -> int:
    """Incrementally fetch only new frames from S3 that aren't already cached.

    Checks for the most recent *count* timestamps, skips those already on
    disk, fetches/decodes/caches only the new ones, pre-renders atlas tiles,
    and computes motion fields for new consecutive pairs.

    Returns the number of newly fetched frames.
    """
    from concurrent.futures import ThreadPoolExecutor

    ref_keys = list_tilt_files("00.50", count=count)
    if not ref_keys:
        return 0

    new_keys = []
    for key in ref_keys:
        ts = _timestamp_from_key(key)
        if ts and not disk_cache.has_tilt_grids(ts):
            new_keys.append(key)

    if not new_keys:
        return 0

    logger.info("Refresh: %d new frames to fetch", len(new_keys))
    ordered = list(reversed(new_keys))  # oldest-first

    fetched = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ref_key in ordered:
            result = _build_frame(ref_key, pool=pool)
            if result is None:
                continue
            timestamp, sparse_grids, meta = result
            tilt_cache.put(timestamp, sparse_grids, meta)
            fetched += 1
            logger.info("  Refresh: fetched %s (%d tilts)", timestamp, len(sparse_grids))

    if fetched == 0:
        return 0

    tile_coords_z4 = _conus_tile_coords(z=4)
    tile_coords_z5 = _conus_tile_coords(z=5)
    entries = disk_cache.list_tilt_grid_timestamps()
    recent = entries[-fetched:]
    atlas_count = _prerender_atlas_tiles(recent, tile_coords_z4)
    atlas_count += _prerender_atlas_tiles(recent, tile_coords_z5)
    if atlas_count:
        logger.info("  Refresh: pre-rendered %d atlas tiles", atlas_count)

    motion_count = compute_all_motion()
    if motion_count:
        logger.info("  Refresh: computed %d motion fields", motion_count)

    return fetched


def purge_stale_data(max_age_hours: float = 3.0, max_frames: int = 60) -> int:
    """Remove frames older than *max_age_hours* from disk, memory, and ts_list.

    Also trims disk cache to at most *max_frames* entries.
    Returns the total number of entries removed.
    """
    from .tiles import atlas_tile_cache

    removed = disk_cache.evict_older_than(hours=max_age_hours)

    entries = disk_cache.list_tilt_grid_timestamps()
    if len(entries) > max_frames:
        excess = entries[: len(entries) - max_frames]
        for entry in excess:
            ts = entry.get("timestamp")
            if ts:
                disk_cache.evict_timestamp(ts)
                removed += 1

    if removed:
        disk_cache.invalidate_ts_list_cache()
        atlas_tile_cache.clear()

    return removed


def invalidate_all() -> None:
    """Clear all in-memory caches."""
    tilt_cache.clear()
    from .tiles import atlas_tile_cache
    atlas_tile_cache.clear()
    disk_cache.invalidate_ts_list_cache()
