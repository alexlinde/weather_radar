"""
MRMS data access: list S3 files, download, decompress, decode, and clip to NYC.

Fetch pipeline (three-tier waterfall):
  1. In-memory cache (backend.cache)
  2. Disk cache (backend.disk_cache) — persists across server restarts
  3. NOAA S3 bucket (noaa-mrms-pds)
"""

from __future__ import annotations

import functools
import gzip
import logging
from typing import Any

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config

from . import cache as _cache
from . import disk_cache
from .grib2.decoder import decode_grib2

logger = logging.getLogger(__name__)

BUCKET = "noaa-mrms-pds"
COMPOSITE_PRODUCT = "CONUS/MergedReflectivityQCComposite_00.50"

NYC_BBOX = {
    "north": 42.0,
    "south": 39.44,
    "east": -72.67,
    "west": -75.23,
}


@functools.lru_cache(maxsize=1)
def _s3_client():
    return boto3.client(
        "s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
    )


def list_latest_files(product: str = COMPOSITE_PRODUCT, count: int = 10) -> list[str]:
    """
    List the most recent GRIB2 files for `product` in the MRMS S3 bucket.

    Scans today first, then yesterday, accumulating until we have `count`
    files so we span across the UTC day boundary.
    Returns up to `count` keys sorted newest-first.
    """
    from datetime import datetime, timedelta, timezone

    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
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


def fetch_raw(s3_key: str) -> bytes:
    """
    Get raw compressed .grib2.gz bytes for a key.
    Checks disk cache first, falls back to S3 download.
    """
    cached = disk_cache.get(s3_key)
    if cached is not None:
        return cached

    logger.info("Downloading s3://%s/%s", BUCKET, s3_key)
    s3 = _s3_client()
    response = s3.get_object(Bucket=BUCKET, Key=s3_key)
    compressed = response["Body"].read()
    logger.info("Downloaded %d bytes", len(compressed))

    disk_cache.put(s3_key, compressed)
    return compressed


def decode_and_clip(
    raw_gz: bytes, bbox: dict | None = None
) -> tuple[np.ndarray, dict[str, Any]]:
    """Decompress, decode GRIB2, mask sentinels, clip to bounding box."""
    bbox = bbox or NYC_BBOX
    raw = gzip.decompress(raw_gz)
    metadata, grid = decode_grib2(raw)
    grid = mask_sentinel_values(grid)
    clipped, clipped_meta = clip_to_bbox(grid, metadata, bbox)
    return clipped, clipped_meta


def fetch_and_decode(
    s3_key: str, bbox: dict | None = None
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    Full pipeline for a single frame.
    Cache waterfall: memory -> decoded disk -> raw disk -> S3 -> decode -> clip.
    """
    bbox = bbox or NYC_BBOX
    entry = _cache.get(s3_key)
    if entry is not None:
        return entry.grid, entry.metadata

    decoded = disk_cache.get_decoded(s3_key)
    if decoded is not None:
        grid, metadata = decoded
        _cache.store(s3_key, metadata, grid)
        return grid, metadata

    raw_gz = fetch_raw(s3_key)
    grid, metadata = decode_and_clip(raw_gz, bbox)
    disk_cache.put_decoded(s3_key, grid, metadata)
    _cache.store(s3_key, metadata, grid)
    return grid, metadata


def get_recent_frames(count: int = 20) -> int:
    """
    Fetch and cache the `count` most recent frames.
    Uses ThreadPoolExecutor for parallel decoding, then inserts into the
    memory cache in chronological order.
    Returns number of frames now in cache.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    keys = list_latest_files(count=count)
    if not keys:
        logger.warning("No MRMS files found in S3 bucket")
        return _cache.frame_count()

    # oldest-first ordering for cache insertion
    ordered_keys = list(reversed(keys))
    to_fetch = [k for k in ordered_keys if not _cache.has(k)]

    if not to_fetch:
        logger.info("All %d frames already cached", len(ordered_keys))
        return _cache.frame_count()

    logger.info("Loading %d frames (%d need decoding)…", len(ordered_keys), len(to_fetch))

    results: dict[str, tuple[np.ndarray, dict[str, Any]]] = {}

    def _decode_one(key: str) -> tuple[str, np.ndarray, dict[str, Any]]:
        grid, meta = fetch_and_decode(key)
        return key, grid, meta

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_decode_one, k): k for k in to_fetch}
        done = 0
        for f in as_completed(futures):
            done += 1
            try:
                key, grid, meta = f.result()
                results[key] = (grid, meta)
                logger.info("  [%d/%d] decoded %s", done, len(to_fetch), key.split("/")[-1])
            except Exception:
                logger.exception("Failed to decode %s, skipping", futures[f])

    # Insert into memory cache in chronological order
    for key in ordered_keys:
        if key in results:
            grid, meta = results[key]
            _cache.store(key, meta, grid)
        elif not _cache.has(key):
            pass  # skip failed keys

    logger.info("Frame cache now has %d frames", _cache.frame_count())
    return _cache.frame_count()


# ── Helpers (unchanged from Phase 1) ─────────────────────────────────────────


def clip_to_bbox(
    data: np.ndarray,
    metadata: dict,
    bbox: dict | None = None,
) -> tuple[np.ndarray, dict]:
    """
    Clip a decoded GRIB2 grid to the given bounding box.

    Assumes row 0 = northernmost latitude (scanning direction already corrected).
    """
    bbox = bbox or NYC_BBOX
    north = metadata["north"]
    south = metadata["south"]
    west = metadata["west"]
    east = metadata["east"]
    Nj = metadata["Nj"]
    Ni = metadata["Ni"]
    Dj = metadata["Dj"]
    Di = metadata["Di"]

    row_start = max(0, int((north - bbox["north"]) / Dj))
    row_end = min(Nj, int((north - bbox["south"]) / Dj) + 1)
    col_start = max(0, int((bbox["west"] - west) / Di))
    col_end = min(Ni, int((bbox["east"] - west) / Di) + 1)

    clipped = data[row_start:row_end, col_start:col_end]

    clipped_north = north - row_start * Dj
    clipped_south = north - (row_end - 1) * Dj
    clipped_west = west + col_start * Di
    clipped_east = west + (col_end - 1) * Di

    clipped_meta = {
        **metadata,
        "north": clipped_north,
        "south": clipped_south,
        "west": clipped_west,
        "east": clipped_east,
        "Nj": clipped.shape[0],
        "Ni": clipped.shape[1],
    }

    logger.info(
        "Clipped grid: %dx%d -> %dx%d  bounds: N%.3f S%.3f W%.3f E%.3f",
        Nj,
        Ni,
        clipped.shape[0],
        clipped.shape[1],
        clipped_north,
        clipped_south,
        clipped_west,
        clipped_east,
    )
    return clipped, clipped_meta


def mask_sentinel_values(data: np.ndarray, threshold: float = -30.0) -> np.ndarray:
    """Replace MRMS sentinel values (e.g. -999) with NaN, in-place."""
    data[data < threshold] = np.nan
    return data


# Keep for backward compat (used by test scripts)
def fetch_grib2(s3_key: str) -> bytes:
    """Download a .grib2.gz file from S3 and return decompressed GRIB2 bytes."""
    raw_gz = fetch_raw(s3_key)
    return gzip.decompress(raw_gz)


def get_latest_frame(bbox: dict | None = None) -> tuple[np.ndarray, dict]:
    """Fetch, decode, and clip the latest MRMS composite reflectivity frame."""
    bbox = bbox or NYC_BBOX
    keys = list_latest_files(COMPOSITE_PRODUCT, count=5)
    if not keys:
        raise RuntimeError("No MRMS files found in S3 bucket")
    return fetch_and_decode(keys[0], bbox)
