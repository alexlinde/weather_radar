"""
TMS tile rendering for CONUS radar composites.

Converts slippy-map tile coordinates (z/x/y) to geographic bounds,
extracts the corresponding region from a CONUS composite grid,
colorizes with the NWS reflectivity scale, and returns a 256x256 PNG.

Tiles follow the standard {z}/{x}/{y} convention used by Leaflet,
MapLibre, OpenLayers, and other map clients.
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image

from .render import colorize_grid

TILE_SIZE = 256
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


# ── Pre-computed transparent tile ────────────────────────────────────────────


_TRANSPARENT_TILE: bytes | None = None


def _get_transparent_tile() -> bytes:
    global _TRANSPARENT_TILE
    if _TRANSPARENT_TILE is None:
        img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        buf.seek(0)
        _TRANSPARENT_TILE = buf.read()
    return _TRANSPARENT_TILE


# ── Tile renderer ────────────────────────────────────────────────────────────


def render_tile(
    grid: np.ndarray,
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Render a 256x256 PNG tile from a CONUS composite grid.

    Returns a transparent PNG when the tile falls entirely outside the grid.
    Handles partial overlap (grid covers only part of the tile).
    """
    tb = tile_bounds(z, x, y)

    grid_n = metadata["north"]
    grid_s = metadata["south"]
    grid_w = metadata["west"]
    grid_e = metadata["east"]
    Dj = metadata["Dj"]
    Di = metadata["Di"]

    overlap_n = min(tb["north"], grid_n)
    overlap_s = max(tb["south"], grid_s)
    overlap_w = max(tb["west"], grid_w)
    overlap_e = min(tb["east"], grid_e)

    if overlap_n <= overlap_s or overlap_e <= overlap_w:
        return _get_transparent_tile()

    row_start = max(0, int(round((grid_n - overlap_n) / Dj)))
    row_end = min(grid.shape[0], int(round((grid_n - overlap_s) / Dj)))
    col_start = max(0, int(round((overlap_w - grid_w) / Di)))
    col_end = min(grid.shape[1], int(round((overlap_e - grid_w) / Di)))

    if row_start >= row_end or col_start >= col_end:
        return _get_transparent_tile()

    subgrid = grid[row_start:row_end, col_start:col_end]

    tile_lon_span = tb["east"] - tb["west"]
    tile_lat_span = tb["north"] - tb["south"]

    px_left = int(round((overlap_w - tb["west"]) / tile_lon_span * TILE_SIZE))
    px_right = int(round((overlap_e - tb["west"]) / tile_lon_span * TILE_SIZE))
    px_top = int(round((tb["north"] - overlap_n) / tile_lat_span * TILE_SIZE))
    px_bottom = int(round((tb["north"] - overlap_s) / tile_lat_span * TILE_SIZE))

    target_w = max(1, px_right - px_left)
    target_h = max(1, px_bottom - px_top)

    rgba = colorize_grid(subgrid)
    img_part = Image.fromarray(rgba, mode="RGBA")
    resample = Image.NEAREST if max(subgrid.shape) < 64 else Image.BILINEAR
    img_part = img_part.resize((target_w, target_h), resample)

    full_coverage = (px_left == 0 and px_top == 0
                     and target_w == TILE_SIZE and target_h == TILE_SIZE)
    if full_coverage:
        buf = io.BytesIO()
        img_part.save(buf, format="PNG", optimize=False)
        buf.seek(0)
        return buf.read()

    tile_img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    tile_img.paste(img_part, (px_left, px_top))

    buf = io.BytesIO()
    tile_img.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()


# ── Tile cache ───────────────────────────────────────────────────────────────


class TileCache:
    """Thread-safe LRU cache for rendered tile PNGs."""

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


tile_cache = TileCache()
