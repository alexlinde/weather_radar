"""
TMS atlas tile rendering for CONUS radar data.

Atlas tiles are 256×2048 grayscale PNGs packing 8 tilt levels vertically.
Pixel encoding: uint8 = round((dBZ + 30) * 2), 0 = no echo.
Used by the custom WebGL radar layer for GPU-side colorisation.
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from typing import Any

import numpy as np
import scipy.sparse as sp
from PIL import Image

MIN_ZOOM = 3
MAX_ZOOM = 8
_TILE_CACHE_MAX = 2000


# ── Tile math ────────────────────────────────────────────────────────────────


def tile_bounds(z: int, x: int, y: int) -> dict[str, float]:
    """Convert slippy-map tile coordinates to a geographic bounding box (EPSG:4326)."""
    n = 2.0 ** z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return {"north": lat_max, "south": lat_min, "west": lon_min, "east": lon_max}


def _grid_overlap(
    tb: dict[str, float], meta: dict[str, Any]
) -> tuple[int, int, int, int] | None:
    """Compute grid row/col slice indices for the overlap between tile and grid bounds.

    Returns (row_start, row_end, col_start, col_end) or None if no overlap.
    """
    grid_n = meta["north"]
    grid_s = meta["south"]
    grid_w = meta["west"]
    grid_e = meta["east"]
    Dj = meta["Dj"]
    Di = meta["Di"]

    overlap_n = min(tb["north"], grid_n)
    overlap_s = max(tb["south"], grid_s)
    overlap_w = max(tb["west"], grid_w)
    overlap_e = min(tb["east"], grid_e)

    if overlap_n <= overlap_s or overlap_e <= overlap_w:
        return None

    Nj = int(meta["Nj"])
    Ni = int(meta["Ni"])
    row_start = max(0, math.floor((grid_n - overlap_n) / Dj))
    row_end = min(Nj, math.ceil((grid_n - overlap_s) / Dj))
    col_start = max(0, math.floor((overlap_w - grid_w) / Di))
    col_end = min(Ni, math.ceil((overlap_e - grid_w) / Di))

    if row_start >= row_end or col_start >= col_end:
        return None

    return row_start, row_end, col_start, col_end


# ── Voxel tile renderer ──────────────────────────────────────────────────────


# ── Tile cache ────────────────────────────────────────────────────────────────


class TileCache:
    """Thread-safe LRU cache for rendered tile data (PNG bytes)."""

    def __init__(self, max_size: int = _TILE_CACHE_MAX) -> None:
        self._max = max_size
        self._data: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
            return None

    def put(self, key: tuple, value: bytes) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return
            if len(self._data) >= self._max:
                self._data.popitem(last=False)
            self._data[key] = value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# ── Atlas tile renderer ───────────────────────────────────────────────────────

ATLAS_BAND_SIZE = 256
ATLAS_NUM_BANDS = 8

TILT_ORDER = ["00.50", "01.50", "02.50", "03.50", "05.00", "07.00", "10.00", "14.00"]

_EMPTY_ATLAS: bytes | None = None


def _get_empty_atlas() -> bytes:
    """Return a cached all-zero 256×2048 grayscale PNG (no echo)."""
    global _EMPTY_ATLAS
    if _EMPTY_ATLAS is None:
        arr = np.zeros((ATLAS_BAND_SIZE * ATLAS_NUM_BANDS, ATLAS_BAND_SIZE), dtype=np.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr, mode="L").save(buf, format="PNG", compress_level=1)
        _EMPTY_ATLAS = buf.getvalue()
    return _EMPTY_ATLAS


def _dbz_to_uint8(grid: np.ndarray) -> np.ndarray:
    """Encode dBZ values to uint8: val = round((dBZ + 30) * 2), 0 = no echo."""
    mask = np.isnan(grid) | (grid == 0)
    encoded = np.where(mask, 0.0, np.round((grid + 30.0) * 2.0))
    np.clip(encoded, 0, 255, out=encoded)
    return encoded.astype(np.uint8)


def _resample_to_band(
    dense: np.ndarray, target_h: int = ATLAS_BAND_SIZE, target_w: int = ATLAS_BAND_SIZE,
) -> np.ndarray:
    """Nearest-neighbor resample a 2D array to target_h × target_w."""
    h, w = dense.shape
    if h == target_h and w == target_w:
        return dense
    row_idx = np.linspace(0, h - 1, target_h).astype(int)
    col_idx = np.linspace(0, w - 1, target_w).astype(int)
    return dense[np.ix_(row_idx, col_idx)]


def render_atlas_tile(
    sparse_grids: dict[str, sp.csr_matrix],
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Render an atlas tile: 256×2048 grayscale PNG with 8 tilt bands.

    Each 256×256 band holds dBZ data for one tilt level, encoded as uint8.
    Data is placed at the correct geographic position within each band —
    partial-coverage tiles (e.g. at CONUS edges) get zeros in uncovered areas.
    Returns PNG bytes.
    """
    tb = tile_bounds(z, x, y)
    overlap = _grid_overlap(tb, metadata)
    if overlap is None:
        return _get_empty_atlas()

    row_start, row_end, col_start, col_end = overlap

    grid_n = metadata["north"]
    grid_w = metadata["west"]
    Di = metadata["Di"]
    Dj = metadata["Dj"]

    # Geographic bounds of the overlap region within the MRMS grid
    data_n = grid_n - row_start * Dj
    data_s = grid_n - row_end * Dj
    data_w = grid_w + col_start * Di
    data_e = grid_w + col_end * Di

    tile_w = tb["west"]
    tile_e = tb["east"]
    tile_n = tb["north"]
    tile_s = tb["south"]
    tile_lon_span = tile_e - tile_w
    tile_lat_span = tile_n - tile_s

    # Pixel coordinates within the 256×256 band where data should be placed
    px_left = int(round((data_w - tile_w) / tile_lon_span * ATLAS_BAND_SIZE))
    px_right = int(round((data_e - tile_w) / tile_lon_span * ATLAS_BAND_SIZE))
    px_top = int(round((tile_n - data_n) / tile_lat_span * ATLAS_BAND_SIZE))
    px_bottom = int(round((tile_n - data_s) / tile_lat_span * ATLAS_BAND_SIZE))

    px_left = max(0, min(ATLAS_BAND_SIZE, px_left))
    px_right = max(0, min(ATLAS_BAND_SIZE, px_right))
    px_top = max(0, min(ATLAS_BAND_SIZE, px_top))
    px_bottom = max(0, min(ATLAS_BAND_SIZE, px_bottom))

    dest_w = px_right - px_left
    dest_h = px_bottom - px_top
    if dest_w <= 0 or dest_h <= 0:
        return _get_empty_atlas()

    atlas = np.zeros((ATLAS_BAND_SIZE * ATLAS_NUM_BANDS, ATLAS_BAND_SIZE), dtype=np.uint8)

    for band_idx, tilt in enumerate(TILT_ORDER):
        sgrid = sparse_grids.get(tilt)
        if sgrid is None:
            continue

        sub = sgrid[row_start:row_end, col_start:col_end].toarray().astype(np.float32)
        sub[sub == 0] = np.nan

        encoded = _dbz_to_uint8(sub)
        resampled = _resample_to_band(encoded, dest_h, dest_w)

        y_off = band_idx * ATLAS_BAND_SIZE
        atlas[y_off + px_top : y_off + px_bottom, px_left : px_right] = resampled

    if not atlas.any():
        return _get_empty_atlas()

    buf = io.BytesIO()
    Image.fromarray(atlas, mode="L").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


atlas_tile_cache = TileCache(max_size=_TILE_CACHE_MAX)
