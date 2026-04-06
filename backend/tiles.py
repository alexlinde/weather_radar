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



def render_atlas_tile(
    sparse_grids: dict[str, sp.csr_matrix],
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Render an atlas tile: 256×2048 grayscale PNG with 8 tilt bands.

    Each 256×256 band holds dBZ data for one tilt level, encoded as uint8.
    Uses Mercator-correct per-pixel sampling: each pixel row's latitude is
    computed from its Mercator Y position, ensuring adjacent tiles produce
    identical values at their shared boundary.
    Returns PNG bytes.
    """
    tb = tile_bounds(z, x, y)

    grid_n = metadata["north"]
    grid_s = metadata["south"]
    grid_w = metadata["west"]
    grid_e = metadata["east"]

    if tb["south"] >= grid_n or tb["north"] <= grid_s:
        return _get_empty_atlas()
    if tb["east"] <= grid_w or tb["west"] >= grid_e:
        return _get_empty_atlas()

    Dj = metadata["Dj"]
    Di = metadata["Di"]
    Nj = int(metadata["Nj"])
    Ni = int(metadata["Ni"])
    n_pow = 2.0 ** z

    # Pixel positions spanning [0, 1] inclusive — ensures boundary pixels
    # from adjacent tiles sample the exact same lat/lon, preventing seams.
    t = np.linspace(0.0, 1.0, ATLAS_BAND_SIZE)

    # Longitude per pixel column (linear in Mercator X = linear in lon)
    lons = tb["west"] + t * (tb["east"] - tb["west"])

    # Latitude per pixel row (via Mercator Y → latitude)
    merc_y0 = y / n_pow
    merc_y1 = (y + 1) / n_pow
    merc_ys = merc_y0 + t * (merc_y1 - merc_y0)
    lats = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * merc_ys))))

    # Nearest-neighbour grid indices
    col_idx = np.round((lons - grid_w) / Di).astype(int)
    row_idx = np.round((grid_n - lats) / Dj).astype(int)

    col_ok = (col_idx >= 0) & (col_idx < Ni)
    row_ok = (row_idx >= 0) & (row_idx < Nj)

    if not np.any(row_ok) or not np.any(col_ok):
        return _get_empty_atlas()

    col_safe = np.clip(col_idx, 0, Ni - 1)
    row_safe = np.clip(row_idx, 0, Nj - 1)

    r_min = int(row_safe[row_ok].min())
    r_max = int(row_safe[row_ok].max()) + 1
    c_min = int(col_safe[col_ok].min())
    c_max = int(col_safe[col_ok].max()) + 1

    local_row = np.clip(row_safe - r_min, 0, r_max - r_min - 1)
    local_col = np.clip(col_safe - c_min, 0, c_max - c_min - 1)
    invalid_mask = ~(row_ok[:, np.newaxis] & col_ok[np.newaxis, :])

    atlas = np.zeros((ATLAS_BAND_SIZE * ATLAS_NUM_BANDS, ATLAS_BAND_SIZE), dtype=np.uint8)

    for band_idx, tilt in enumerate(TILT_ORDER):
        sgrid = sparse_grids.get(tilt)
        if sgrid is None:
            continue

        sub = sgrid[r_min:r_max, c_min:c_max].toarray().astype(np.float32)
        sub[sub == 0] = np.nan

        band = sub[local_row][:, local_col]
        band[invalid_mask] = np.nan

        encoded = _dbz_to_uint8(band)
        y_off = band_idx * ATLAS_BAND_SIZE
        atlas[y_off : y_off + ATLAS_BAND_SIZE] = encoded

    if not atlas.any():
        return _get_empty_atlas()

    buf = io.BytesIO()
    Image.fromarray(atlas, mode="L").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


atlas_tile_cache = TileCache(max_size=_TILE_CACHE_MAX)
