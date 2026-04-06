"""
TMS tile rendering for CONUS radar data (2D composites and 3D voxel tiles).

2D tiles: extract subgrid from composite → colorize → 256x256 PNG.
3D tiles: extract subregion from each tilt → downsample → filter → voxel JSON.

Both tile types follow the standard {z}/{x}/{y} convention.
"""

from __future__ import annotations

import io
import math
import struct
import threading
from collections import OrderedDict
from typing import Any

import numpy as np
import scipy.sparse as sp
from PIL import Image

from .render import NWS_DBZ_COLORS, colorize_grid

TILE_SIZE = 256
MIN_ZOOM = 3
MAX_ZOOM = 8
_TILE_CACHE_MAX = 2000
_VOXEL_TILE_CACHE_MAX = 2000

TILT_TO_HEIGHT_KM: dict[str, float] = {
    "00.50": 1.0,
    "01.50": 2.0,
    "02.50": 3.5,
    "03.50": 5.0,
    "05.00": 7.0,
    "07.00": 9.0,
    "10.00": 12.0,
    "14.00": 15.0,
}


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
    row_start = max(0, int(round((grid_n - overlap_n) / Dj)))
    row_end = min(Nj, int(round((grid_n - overlap_s) / Dj)))
    col_start = max(0, int(round((overlap_w - grid_w) / Di)))
    col_end = min(Ni, int(round((overlap_e - grid_w) / Di)))

    if row_start >= row_end or col_start >= col_end:
        return None

    return row_start, row_end, col_start, col_end


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


# ── 2D tile renderer ─────────────────────────────────────────────────────────


def render_tile(
    grid: np.ndarray,
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Render a 256x256 PNG tile from a CONUS composite grid."""
    tb = tile_bounds(z, x, y)
    overlap = _grid_overlap(tb, metadata)
    if overlap is None:
        return _get_transparent_tile()

    row_start, row_end, col_start, col_end = overlap
    subgrid = grid[row_start:row_end, col_start:col_end]

    tile_lon_span = tb["east"] - tb["west"]
    tile_lat_span = tb["north"] - tb["south"]
    Di = metadata["Di"]
    Dj = metadata["Dj"]
    grid_n = metadata["north"]
    grid_w = metadata["west"]

    overlap_n = grid_n - row_start * Dj
    overlap_s = grid_n - row_end * Dj
    overlap_w = grid_w + col_start * Di
    overlap_e = grid_w + col_end * Di

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


# ── 3D voxel tile renderer ──────────────────────────────────────────────────


def _downsample_step(z: int) -> int:
    """Spatial downsample factor based on zoom level.

    At low zoom (CONUS view), skip grid cells to keep voxel counts manageable.
    """
    return max(1, 2 ** (7 - z))


# At low zoom, only render a subset of tilts to halve voxel count
_LOW_ZOOM_TILTS = {"00.50", "02.50", "05.00", "10.00"}
_MAX_VOXELS_PER_TILE = 40_000

_EMPTY_BIN_TILE = struct.pack("<I", 0)
DELTA_UNCHANGED = struct.pack("<I", 0xFFFFFFFF)


def colorize_voxels(dbz_values: np.ndarray) -> np.ndarray:
    """Vectorized dBZ → RGBA for voxel points. Returns Nx4 uint8 array."""
    n = len(dbz_values)
    colors = np.zeros((n, 4), dtype=np.uint8)
    alphas = np.clip(
        np.round(100 + (dbz_values - 10) * (155 / 50)), 100, 255
    ).astype(np.uint8)
    for min_dbz, max_dbz, r, g, b in NWS_DBZ_COLORS:
        mask = (dbz_values >= min_dbz) & (dbz_values < max_dbz)
        colors[mask, :3] = [r, g, b]
    colors[dbz_values >= 75, :3] = [200, 200, 255]
    has_color = colors[:, :3].any(axis=1)
    colors[has_color, 3] = alphas[has_color]
    return colors


def _extract_voxels(
    sparse_grids: dict[str, sp.csr_matrix],
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> np.ndarray | None:
    """Core voxel extraction shared by JSON and binary renderers.

    Returns Nx4 float array of [lon, lat, altitude_m, dbz] or None.
    """
    tb = tile_bounds(z, x, y)
    overlap = _grid_overlap(tb, metadata)
    if overlap is None:
        return None

    row_start, row_end, col_start, col_end = overlap
    step = _downsample_step(z)
    min_dbz = 15.0 if z <= 5 else 10.0
    grid_n = metadata["north"]
    grid_w = metadata["west"]
    Dj = metadata["Dj"]
    Di = metadata["Di"]

    use_low_zoom_tilts = z <= 5
    all_voxels: list[np.ndarray] = []
    total_count = 0

    for tilt, sgrid in sparse_grids.items():
        if use_low_zoom_tilts and tilt not in _LOW_ZOOM_TILTS:
            continue

        height_km = TILT_TO_HEIGHT_KM.get(tilt)
        if height_km is None:
            continue
        height_m = height_km * 1000.0

        sub = sgrid[row_start:row_end, col_start:col_end].toarray()
        if step > 1:
            sub = sub[::step, ::step]

        sub[sub == 0] = np.nan
        mask = (~np.isnan(sub)) & (sub >= min_dbz)
        rows, cols = np.where(mask)
        if len(rows) == 0:
            continue

        lons = np.round(grid_w + (col_start + cols * step) * Di + Di / 2, 5)
        lats = np.round(grid_n - (row_start + rows * step) * Dj - Dj / 2, 5)
        alts = np.full(len(rows), height_m)
        dbz = np.round(sub[rows, cols], 1)

        all_voxels.append(np.column_stack([lons, lats, alts, dbz]))
        total_count += len(rows)

        if total_count >= _MAX_VOXELS_PER_TILE:
            break

    if not all_voxels:
        return None

    result = np.vstack(all_voxels)
    if len(result) > _MAX_VOXELS_PER_TILE:
        result = result[:_MAX_VOXELS_PER_TILE]
    return result


def render_voxel_tile(
    sparse_grids: dict[str, sp.csr_matrix],
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> list[list[float]]:
    """Extract voxels as JSON-serializable list of [lon, lat, altitude_m, dbz]."""
    result = _extract_voxels(sparse_grids, metadata, z, x, y)
    return result.tolist() if result is not None else []


def render_voxel_tile_binary(
    sparse_grids: dict[str, sp.csr_matrix],
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Pack voxels as binary: [uint32 count][float32 positions][uint8 colors].

    Count 0xFFFFFFFF is reserved as the delta "unchanged" sentinel.
    Positions are 3 × float32 (lon, lat, alt_m) per voxel.
    Colors are 4 × uint8 (R, G, B, A) per voxel, pre-computed from dBZ.
    """
    result = _extract_voxels(sparse_grids, metadata, z, x, y)
    if result is None:
        return _EMPTY_BIN_TILE

    count = len(result)
    positions = result[:, :3].astype(np.float32)
    colors = colorize_voxels(result[:, 3])
    return struct.pack("<I", count) + positions.tobytes() + colors.tobytes()


# ── Tile caches ──────────────────────────────────────────────────────────────


class TileCache:
    """Thread-safe LRU cache for rendered tile data (PNG bytes or JSON bytes)."""

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
voxel_tile_cache = TileCache(max_size=_VOXEL_TILE_CACHE_MAX)
bin_tile_cache = TileCache(max_size=_VOXEL_TILE_CACHE_MAX)
