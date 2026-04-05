#!/usr/bin/env python3
"""
Standalone script: download the latest MRMS composite reflectivity frame,
decode it, print diagnostic info, and save the raw .gz file to tests/fixtures/.

Run from the project root:
    python scripts/test_fetch.py
"""

from __future__ import annotations

import sys
import os
import gzip
import pathlib

# Allow importing from backend/ when run from the project root
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config

from backend.mrms import BUCKET, COMPOSITE_PRODUCT, list_latest_files, fetch_grib2, clip_to_bbox, mask_sentinel_values
from backend.grib2.decoder import decode_grib2

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> None:
    print("=" * 60)
    print("MRMS Fetch + Decode Test")
    print("=" * 60)

    # --- List available files ---
    print(f"\nListing files in s3://{BUCKET}/{COMPOSITE_PRODUCT}/YYYYMMDD/ …")
    keys = list_latest_files(COMPOSITE_PRODUCT, count=5)
    if not keys:
        print("ERROR: No files found in bucket.")
        sys.exit(1)

    print(f"Found {len(keys)} files. Latest:")
    for k in keys[:5]:
        print(f"  {k}")

    latest_key = keys[0]

    # --- Download + decompress ---
    print(f"\nDownloading: {latest_key}")
    s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
    response = s3.get_object(Bucket=BUCKET, Key=latest_key)
    compressed = response["Body"].read()
    print(f"Compressed size: {len(compressed) / 1024:.1f} KB")

    # Save the compressed file to fixtures/
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    fixture_name = pathlib.Path(latest_key).name  # e.g. MRMS_Merged...grib2.gz
    fixture_path = FIXTURES_DIR / fixture_name
    fixture_path.write_bytes(compressed)
    print(f"Saved fixture to: {fixture_path}")

    # --- Decompress + decode ---
    print("\nDecompressing…")
    raw = gzip.decompress(compressed)
    print(f"Decompressed size: {len(raw) / 1024:.1f} KB")

    print("Decoding GRIB2…")
    metadata, grid = decode_grib2(raw)
    grid = mask_sentinel_values(grid)

    print("\n--- CONUS Grid ---")
    print(f"  Shape:      {grid.shape} (rows × cols = Nj × Ni)")
    print(f"  Timestamp:  {metadata['timestamp']}")
    print(f"  North:      {metadata['north']:.4f}°")
    print(f"  South:      {metadata['south']:.4f}°")
    print(f"  West:       {metadata['west']:.4f}°")
    print(f"  East:       {metadata['east']:.4f}°")
    print(f"  Di/Dj:      {metadata['Di']:.4f}° / {metadata['Dj']:.4f}°")
    print(f"  Packing:    template {metadata['packing_template']}")

    valid = grid[~np.isnan(grid)]
    print(f"\n  Total grid points:  {grid.size:,}")
    print(f"  Valid (non-NaN):    {len(valid):,}")
    print(f"  Min dBZ:            {valid.min():.2f}")
    print(f"  Max dBZ:            {valid.max():.2f}")
    print(f"  Mean dBZ (valid):   {valid.mean():.2f}")
    print(f"  Points > 5 dBZ:     {(valid > 5).sum():,}")
    print(f"  Points > 35 dBZ:    {(valid > 35).sum():,}")

    # --- Clip to NYC ---
    print("\n--- NYC Clip ---")
    from backend.mrms import NYC_BBOX
    clipped, cmeta = clip_to_bbox(grid, metadata, NYC_BBOX)
    print(f"  Shape:  {clipped.shape}")
    print(f"  North:  {cmeta['north']:.4f}°")
    print(f"  South:  {cmeta['south']:.4f}°")
    print(f"  West:   {cmeta['west']:.4f}°")
    print(f"  East:   {cmeta['east']:.4f}°")

    cvalid = clipped[~np.isnan(clipped)]
    print(f"  Valid:  {len(cvalid):,}")
    if len(cvalid) > 0:
        print(f"  Min:    {cvalid.min():.2f} dBZ")
        print(f"  Max:    {cvalid.max():.2f} dBZ")
    else:
        print("  (no valid data points in NYC clip — this is normal if there's no precipitation)")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
