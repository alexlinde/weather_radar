"""
Tests for the custom GRIB2 decoder.

Requires a real MRMS fixture file in tests/fixtures/. Run scripts/test_fetch.py
once to download it before running these tests.
"""

from __future__ import annotations

import gzip
import pathlib

import numpy as np
import pytest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _find_fixture() -> pathlib.Path:
    """Return the first .grib2.gz file in tests/fixtures/."""
    files = sorted(FIXTURES_DIR.glob("*.grib2.gz"))
    if not files:
        pytest.skip("No fixture file found in tests/fixtures/. Run scripts/test_fetch.py first.")
    return files[-1]  # newest by sort order


@pytest.fixture(scope="module")
def decoded():
    """Decode the fixture file once and share across tests."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

    from backend.grib2.decoder import decode_grib2
    from backend.mrms import mask_sentinel_values

    fixture = _find_fixture()
    raw = gzip.decompress(fixture.read_bytes())
    metadata, grid = decode_grib2(raw)
    grid = mask_sentinel_values(grid)
    return metadata, grid


class TestMetadata:
    def test_timestamp_present(self, decoded):
        metadata, _ = decoded
        assert metadata["timestamp"] is not None
        # ISO-8601 format: YYYY-MM-DDTHH:MM:SSZ
        assert "T" in metadata["timestamp"]
        assert metadata["timestamp"].endswith("Z")

    def test_grid_dimensions_positive(self, decoded):
        metadata, _ = decoded
        assert metadata["Ni"] > 0
        assert metadata["Nj"] > 0

    def test_conus_lat_bounds(self, decoded):
        metadata, _ = decoded
        # MRMS CONUS covers approximately 20°N to 55°N
        assert 15.0 <= metadata["south"] <= 30.0, f"Unexpected south: {metadata['south']}"
        assert 45.0 <= metadata["north"] <= 60.0, f"Unexpected north: {metadata['north']}"
        assert metadata["south"] < metadata["north"]

    def test_conus_lon_bounds(self, decoded):
        metadata, _ = decoded
        # MRMS CONUS covers approximately -130°W to -60°W
        assert -140.0 <= metadata["west"] <= -120.0, f"Unexpected west: {metadata['west']}"
        assert -70.0 <= metadata["east"] <= -50.0, f"Unexpected east: {metadata['east']}"
        assert metadata["west"] < metadata["east"]

    def test_packing_template_supported(self, decoded):
        metadata, _ = decoded
        assert metadata["packing_template"] in (0, 40, 41), (
            f"Unexpected packing template: {metadata['packing_template']}"
        )

    def test_resolution(self, decoded):
        metadata, _ = decoded
        # MRMS CONUS composite is 0.01° resolution
        assert abs(metadata["Di"] - 0.01) < 1e-4, f"Unexpected Di: {metadata['Di']}"
        assert abs(metadata["Dj"] - 0.01) < 1e-4, f"Unexpected Dj: {metadata['Dj']}"


class TestGrid:
    def test_shape_matches_metadata(self, decoded):
        metadata, grid = decoded
        assert grid.shape == (metadata["Nj"], metadata["Ni"])

    def test_dtype_float64(self, decoded):
        _, grid = decoded
        assert grid.dtype == np.float64

    def test_expected_conus_shape(self, decoded):
        _, grid = decoded
        # MRMS CONUS composite: 3500 rows × 7000 cols at 0.01° resolution
        assert grid.shape == (3500, 7000), f"Unexpected shape: {grid.shape}"

    def test_dbz_value_range(self, decoded):
        _, grid = decoded
        valid = grid[~np.isnan(grid)]
        assert len(valid) > 0, "No valid (non-NaN) data points found"
        # Physical reflectivity is bounded: nothing below -30 (after sentinel masking) or above ~80 dBZ
        assert valid.min() >= -30.0, f"Suspiciously low dBZ: {valid.min()}"
        assert valid.max() <= 80.0, f"Suspiciously high dBZ: {valid.max()}"

    def test_no_unmasked_sentinels(self, decoded):
        _, grid = decoded
        valid = grid[~np.isnan(grid)]
        # After sentinel masking, no values should be at -999 or -99
        assert (valid < -30).sum() == 0, "Unmasked sentinel values found"

    def test_significant_valid_data(self, decoded):
        _, grid = decoded
        valid = grid[~np.isnan(grid)]
        # CONUS should have at least some points with data
        frac_valid = len(valid) / grid.size
        assert frac_valid > 0.0, "All data points are NaN"

    def test_some_precipitation_like_values(self, decoded):
        """CONUS should nearly always have *some* precipitation somewhere."""
        _, grid = decoded
        valid = grid[~np.isnan(grid)]
        points_above_5dbz = (valid > 5).sum()
        # At any given moment there will be rain somewhere over CONUS (usually >> 100k points)
        # Use a very conservative threshold to avoid false failures
        assert points_above_5dbz >= 1, "Expected at least some precipitation over CONUS"
        if points_above_5dbz == 0:
            import warnings
            warnings.warn("No precipitation found in CONUS (unusual but possible in rare conditions)")


class TestNYCClip:
    def test_clip_shape(self, decoded):
        metadata, grid = decoded
        from backend.mrms import clip_to_bbox, NYC_BBOX
        clipped, cmeta = clip_to_bbox(grid, metadata, NYC_BBOX)
        # Should be a small array
        assert clipped.ndim == 2
        assert clipped.shape[0] > 0
        assert clipped.shape[1] > 0
        assert clipped.shape == (cmeta["Nj"], cmeta["Ni"])

    def test_clip_bounds_within_bbox(self, decoded):
        metadata, grid = decoded
        from backend.mrms import clip_to_bbox, NYC_BBOX
        _, cmeta = clip_to_bbox(grid, metadata, NYC_BBOX)
        # Clipped bounds should be within (or very close to) the requested bbox
        assert cmeta["north"] <= NYC_BBOX["north"] + 0.02
        assert cmeta["south"] >= NYC_BBOX["south"] - 0.02
        assert cmeta["west"] >= NYC_BBOX["west"] - 0.02
        assert cmeta["east"] <= NYC_BBOX["east"] + 0.02
