"""
MRMS data access: list S3 files, download, decompress, decode, and clip to NYC.
"""

from __future__ import annotations

import gzip
import logging
from typing import Any

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config

from .grib2.decoder import decode_grib2

logger = logging.getLogger(__name__)

BUCKET = "noaa-mrms-pds"
# Actual bucket layout: CONUS/MergedReflectivityQCComposite_00.50/YYYYMMDD/
COMPOSITE_PRODUCT = "CONUS/MergedReflectivityQCComposite_00.50"

# NYC bounding box (generous — covers all 5 boroughs + NJ + Long Island + Westchester)
NYC_BBOX = {
    "north": 41.0,
    "south": 40.4,
    "east": -73.6,
    "west": -74.3,
}


def _s3_client():
    return boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))


def list_latest_files(product: str = COMPOSITE_PRODUCT, count: int = 10) -> list[str]:
    """
    List the most recent GRIB2 files for `product` in the MRMS S3 bucket.

    The bucket uses date-based subdirectories:
        CONUS/MergedReflectivityQCComposite_00.50/YYYYMMDD/*.grib2.gz

    Scans today's directory first; falls back to yesterday if needed.
    Returns up to `count` keys sorted in descending order (newest first).
    """
    from datetime import datetime, timezone, timedelta

    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    now = datetime.now(timezone.utc)

    for delta in range(3):  # today, yesterday, day-before
        day = now - timedelta(days=delta)
        date_str = day.strftime("%Y%m%d")
        prefix = f"{product}/{date_str}/"

        keys: list[str] = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".grib2.gz"):
                    keys.append(key)

        if keys:
            keys.sort(reverse=True)
            return keys[:count]

    return []


def fetch_grib2(s3_key: str) -> bytes:
    """
    Download a .grib2.gz file from S3 and return decompressed GRIB2 bytes.
    """
    logger.info("Fetching s3://%s/%s", BUCKET, s3_key)
    s3 = _s3_client()
    response = s3.get_object(Bucket=BUCKET, Key=s3_key)
    compressed = response["Body"].read()
    logger.info("Downloaded %d bytes, decompressing…", len(compressed))
    return gzip.decompress(compressed)


def clip_to_bbox(
    data: np.ndarray,
    metadata: dict,
    bbox: dict = NYC_BBOX,
) -> tuple[np.ndarray, dict]:
    """
    Clip a decoded GRIB2 grid to the given bounding box.

    Assumes `data` is a 2D array with row 0 = northernmost latitude
    (i.e., already corrected for scanning direction by the decoder).

    Returns the clipped array and an updated metadata dict.
    """
    north = metadata["north"]
    south = metadata["south"]
    west = metadata["west"]
    east = metadata["east"]
    Nj = metadata["Nj"]
    Ni = metadata["Ni"]
    Dj = metadata["Dj"]
    Di = metadata["Di"]

    # Row 0 = northernmost; latitude decreases as row index increases
    row_start = max(0, int((north - bbox["north"]) / Dj))
    row_end = min(Nj, int((north - bbox["south"]) / Dj) + 1)

    # Column 0 = westernmost; longitude increases as column index increases
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
        "Clipped grid: %dx%d → %dx%d  bounds: N%.3f S%.3f W%.3f E%.3f",
        Nj, Ni,
        clipped.shape[0], clipped.shape[1],
        clipped_north, clipped_south, clipped_west, clipped_east,
    )
    return clipped, clipped_meta


def mask_sentinel_values(data: np.ndarray, threshold: float = -30.0) -> np.ndarray:
    """
    Replace MRMS sentinel "no data" values (e.g. -999, -99) with NaN.

    MRMS uses large negative values to indicate missing or below-threshold data.
    Any value below `threshold` dBZ is physically unrealistic for reflectivity
    and should be treated as missing.
    """
    result = data.copy()
    result[result < threshold] = np.nan
    return result


def get_latest_frame(bbox: dict = NYC_BBOX) -> tuple[np.ndarray, dict]:
    """
    Fetch, decode, and clip the latest MRMS composite reflectivity frame.
    Returns (clipped_grid, metadata). Sentinel values are replaced with NaN.
    """
    keys = list_latest_files(COMPOSITE_PRODUCT, count=5)
    if not keys:
        raise RuntimeError("No MRMS files found in S3 bucket")

    latest_key = keys[0]
    logger.info("Latest MRMS file: %s", latest_key)

    raw = fetch_grib2(latest_key)
    metadata, grid = decode_grib2(raw)

    grid = mask_sentinel_values(grid)

    clipped, clipped_meta = clip_to_bbox(grid, metadata, bbox)
    return clipped, clipped_meta
