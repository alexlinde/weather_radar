"""
MRMS data access: S3 client, file listing, download, decode, and clip.

Low-level utilities used by pipeline.py (the primary data pipeline).
Fetch path: disk cache → S3 bucket (noaa-mrms-pds).
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

from . import disk_cache
from .grib2.decoder import decode_grib2

logger = logging.getLogger(__name__)

BUCKET = "noaa-mrms-pds"

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


def list_latest_files(product: str, count: int = 10) -> list[str]:
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


# ── Helpers ───────────────────────────────────────────────────────────────────


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


