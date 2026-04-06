"""
GRIB2 data unpackers for Templates 5.0, 5.40, and 5.41.

All three templates use the same physical formula after extracting packed integers:
    value = (R + packed_int * 2^E) / 10^D

where R, E, D come from Section 5.
"""

from __future__ import annotations

from io import BytesIO

import numpy as np

from .bitstream import BitstreamReader


def _apply_scale(packed: np.ndarray, R: float, E: int, D: int) -> np.ndarray:
    """Apply the GRIB2 packing formula to convert packed integers to physical values."""
    return (R + packed.astype(np.float32) * (2.0**E)) / (10.0**D)


def _unpack_bitmap(bitmap_bytes: bytes, num_points: int) -> np.ndarray:
    """Unpack a GRIB2 bitmap to a boolean mask of valid data points."""
    bits = np.unpackbits(np.frombuffer(bitmap_bytes, dtype=np.uint8))
    return bits[:num_points]


def _expand_bitmap(
    values: np.ndarray,
    bitmap_mask: np.ndarray | None,
    num_points: int,
) -> np.ndarray:
    """
    If a bitmap mask is present, expand the sparse `values` array (one entry per
    set bit) back to the full `num_points` grid, filling missing points with NaN.
    """
    if bitmap_mask is None:
        if len(values) != num_points:
            raise ValueError(
                f"Expected {num_points} values but got {len(values)} (no bitmap present)"
            )
        return values

    full = np.full(num_points, np.nan, dtype=np.float32)
    full[bitmap_mask == 1] = values
    return full


def unpack_simple(
    section7_bytes: bytes,
    num_points: int,
    bits_per_value: int,
    R: float,
    E: int,
    D: int,
    bitmap_bytes: bytes | None,
) -> np.ndarray:
    """
    Template 5.0 — Simple Packing.
    Reads `num_packed` N-bit unsigned integers from the bitstream, applies
    the scaling formula, then expands using the bitmap.
    """
    bitmap_mask = _unpack_bitmap(bitmap_bytes, num_points) if bitmap_bytes is not None else None
    num_packed = int(bitmap_mask.sum()) if bitmap_mask is not None else num_points

    if bits_per_value == 0:
        physical = np.full(num_packed, R / (10.0**D), dtype=np.float32)
    else:
        reader = BitstreamReader(section7_bytes)
        raw = reader.read_array(bits_per_value, num_packed).astype(np.float32)
        physical = _apply_scale(raw, R, E, D)

    return _expand_bitmap(physical, bitmap_mask, num_points)


def unpack_jpeg2000(
    section7_bytes: bytes,
    num_points: int,
    R: float,
    E: int,
    D: int,
    bitmap_bytes: bytes | None,
) -> np.ndarray:
    """
    Template 5.40 — JPEG2000 Packing.
    Decodes the J2K codestream with Pillow (ships with openjpeg).
    """
    from PIL import Image

    img = Image.open(BytesIO(section7_bytes))
    packed = np.array(img, dtype=np.float32).ravel()

    bitmap_mask = _unpack_bitmap(bitmap_bytes, num_points) if bitmap_bytes is not None else None
    physical = _apply_scale(packed, R, E, D)
    return _expand_bitmap(physical, bitmap_mask, num_points)


def unpack_png(
    section7_bytes: bytes,
    num_points: int,
    R: float,
    E: int,
    D: int,
    bitmap_bytes: bytes | None,
) -> np.ndarray:
    """
    Template 5.41 — PNG Packing.
    Same as JPEG2000 but the embedded data is a PNG image.
    """
    from PIL import Image

    img = Image.open(BytesIO(section7_bytes))
    packed = np.array(img, dtype=np.float32).ravel()

    bitmap_mask = _unpack_bitmap(bitmap_bytes, num_points) if bitmap_bytes is not None else None
    physical = _apply_scale(packed, R, E, D)
    return _expand_bitmap(physical, bitmap_mask, num_points)
