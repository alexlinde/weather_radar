"""
MRMS data access: S3 client, file listing, download.

Low-level utilities used by pipeline.py (the primary data pipeline).
Fetch path: disk cache → S3 bucket (noaa-mrms-pds).
"""

from __future__ import annotations

import functools
import logging

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config

from . import disk_cache

logger = logging.getLogger(__name__)

BUCKET = "noaa-mrms-pds"


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


class S3KeyNotFound(Exception):
    """Raised when an S3 key does not exist (yet)."""


def fetch_raw(s3_key: str) -> bytes:
    """
    Get raw compressed .grib2.gz bytes for a key.
    Checks disk cache first, falls back to S3 download.
    Raises S3KeyNotFound if the key doesn't exist in the bucket.
    """
    cached = disk_cache.get(s3_key)
    if cached is not None:
        return cached

    logger.info("Downloading s3://%s/%s", BUCKET, s3_key)
    s3 = _s3_client()
    try:
        response = s3.get_object(Bucket=BUCKET, Key=s3_key)
    except s3.exceptions.NoSuchKey:
        raise S3KeyNotFound(s3_key)
    compressed = response["Body"].read()
    logger.info("Downloaded %d bytes", len(compressed))

    disk_cache.put(s3_key, compressed)
    return compressed


# ── Helpers ───────────────────────────────────────────────────────────────────


def mask_sentinel_values(data: np.ndarray, threshold: float = -30.0) -> np.ndarray:
    """Replace MRMS sentinel values (e.g. -999) with NaN, in-place."""
    data[data < threshold] = np.nan
    return data


