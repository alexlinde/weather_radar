"""NWS reflectivity color scale and grid-to-PNG rendering."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

NWS_DBZ_COLORS: list[tuple[float, float, int, int, int]] = [
    (5, 10, 64, 192, 64),
    (10, 15, 48, 160, 48),
    (15, 20, 0, 144, 0),
    (20, 25, 0, 120, 0),
    (25, 30, 255, 255, 0),
    (30, 35, 230, 180, 0),
    (35, 40, 255, 100, 0),
    (40, 45, 255, 0, 0),
    (45, 50, 200, 0, 0),
    (50, 55, 180, 0, 120),
    (55, 60, 150, 0, 200),
    (60, 65, 255, 255, 255),
    (65, 75, 200, 200, 255),
]


def colorize_grid(grid: np.ndarray) -> np.ndarray:
    """Convert a 2D dBZ array to an RGBA uint8 array using the NWS color scale.

    Transparent (alpha 0) below 5 dBZ; alpha 220 for active bands.
    """
    rows, cols = grid.shape
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)

    for min_dbz, max_dbz, r, g, b in NWS_DBZ_COLORS:
        mask = (~np.isnan(grid)) & (grid >= min_dbz) & (grid < max_dbz)
        rgba[mask] = [r, g, b, 220]

    rgba[(~np.isnan(grid)) & (grid >= 75)] = [200, 200, 255, 220]
    return rgba


def grid_to_png(grid: np.ndarray) -> bytes:
    """Convert a 2D dBZ array to a PNG with NWS color scale. Transparent below 5 dBZ."""
    rgba = colorize_grid(grid)
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()
