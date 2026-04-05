"""
Top-level GRIB2 decoder.

Usage:
    metadata, grid = decode_grib2(raw_bytes)

`raw_bytes` must be the *decompressed* GRIB2 bytes (call gzip.decompress() first for .gz files).

Returns:
    metadata: dict with keys timestamp, La1, Lo1, La2, Lo2, Ni, Nj, Di, Dj
    grid:     numpy 2-D float64 array of shape (Nj, Ni), dBZ values, NaN for missing
"""

from __future__ import annotations

import numpy as np

from .packing import unpack_simple, unpack_jpeg2000, unpack_png
from .sections import (
    parse_section0,
    parse_section1,
    parse_section3,
    parse_section4,
    parse_section5,
    parse_section6,
    parse_section7,
)


def decode_grib2(raw_bytes: bytes) -> tuple[dict, np.ndarray]:
    """
    Decode a single GRIB2 message and return (metadata, 2D numpy array).

    Only supports:
      - Grid Template 3.0 (regular lat/lon)
      - Data representation templates 5.0, 5.40, 5.41
    """
    data = raw_bytes
    offset = 0

    # ---- Section 0: Indicator ----
    sec0 = parse_section0(data, offset)
    offset += sec0["section_length"]  # 16 bytes

    # ---- Walk remaining sections ----
    sec1 = sec3 = sec4 = sec5 = sec6 = sec7 = None

    while offset < len(data):
        # Check for end marker "7777"
        if data[offset : offset + 4] == b"7777":
            break

        sec_len = int.from_bytes(data[offset : offset + 4], "big")
        sec_num = data[offset + 4]

        if sec_num == 1:
            sec1 = parse_section1(data, offset)
        elif sec_num == 2:
            pass  # Local use section — skip
        elif sec_num == 3:
            sec3 = parse_section3(data, offset)
        elif sec_num == 4:
            sec4 = parse_section4(data, offset)
        elif sec_num == 5:
            sec5 = parse_section5(data, offset)
        elif sec_num == 6:
            sec6 = parse_section6(data, offset)
        elif sec_num == 7:
            sec7 = parse_section7(data, offset)
        else:
            pass  # Unknown section — skip

        offset += sec_len

    # ---- Validate that required sections were found ----
    missing = [name for name, sec in [("1", sec1), ("3", sec3), ("5", sec5), ("6", sec6), ("7", sec7)] if sec is None]
    if missing:
        raise ValueError(f"GRIB2 message is missing required sections: {missing}")

    grid_info = sec3
    pack_info = sec5
    bitmap_bytes = sec6["bitmap"] if sec6["has_bitmap"] else None

    num_points = grid_info["Ni"] * grid_info["Nj"]

    # ---- Unpack data ----
    template = pack_info["template_num"]
    R = pack_info["R"]
    E = pack_info["E"]
    D = pack_info["D"]
    bits = pack_info["bits_per_value"]
    raw7 = sec7["data"]

    if template == 0:
        flat = unpack_simple(raw7, num_points, bits, R, E, D, bitmap_bytes)
    elif template == 40:
        flat = unpack_jpeg2000(raw7, num_points, R, E, D, bitmap_bytes)
    elif template == 41:
        flat = unpack_png(raw7, num_points, R, E, D, bitmap_bytes)
    else:
        raise NotImplementedError(f"Packing template {template} not supported")

    # ---- Reshape to 2D (Nj rows × Ni cols) ----
    Nj = grid_info["Nj"]
    Ni = grid_info["Ni"]
    grid = flat.reshape(Nj, Ni)

    # ---- Handle scanning direction ----
    # scan_j_pos: True means data goes S→N (first row is southernmost).
    # Standard convention for our output is N→S (first row = northernmost).
    if grid_info["scan_j_pos"]:
        grid = np.flipud(grid)

    # scan_i_neg: True means data goes E→W. Standard is W→E.
    if grid_info["scan_i_neg"]:
        grid = np.fliplr(grid)

    # ---- Build metadata ----
    timestamp = None
    if sec1:
        timestamp = (
            f"{sec1['year']:04d}-{sec1['month']:02d}-{sec1['day']:02d}"
            f"T{sec1['hour']:02d}:{sec1['minute']:02d}:{sec1['second']:02d}Z"
        )

    # After scanning-direction correction, La1/Lo1 may no longer be top-left.
    # Compute the actual corner latitudes.
    if grid_info["scan_j_pos"]:
        north = grid_info["La2"]
        south = grid_info["La1"]
    else:
        north = grid_info["La1"]
        south = grid_info["La2"]

    # Longitudes: normalise to [-180, 180]
    def norm_lon(lon: float) -> float:
        return lon if lon <= 180.0 else lon - 360.0

    if grid_info["scan_i_neg"]:
        west = norm_lon(grid_info["Lo2"])
        east = norm_lon(grid_info["Lo1"])
    else:
        west = norm_lon(grid_info["Lo1"])
        east = norm_lon(grid_info["Lo2"])

    metadata = {
        "timestamp": timestamp,
        "La1": grid_info["La1"],
        "Lo1": grid_info["Lo1"],
        "La2": grid_info["La2"],
        "Lo2": grid_info["Lo2"],
        "north": north,
        "south": south,
        "west": west,
        "east": east,
        "Ni": Ni,
        "Nj": Nj,
        "Di": grid_info["Di"],
        "Dj": grid_info["Dj"],
        "scan_j_pos": grid_info["scan_j_pos"],
        "scan_i_neg": grid_info["scan_i_neg"],
        "packing_template": template,
    }

    return metadata, grid
