"""
Motion field computation for frame interpolation.

Computes 2D displacement fields between consecutive radar composites
using FFT-based block matching. Motion vectors are encoded as RGB PNG
for efficient GPU-side semi-Lagrangian advection.

No new dependencies — uses numpy, scipy, Pillow (already in requirements).
"""

from __future__ import annotations

import io
import logging

import numpy as np
import scipy.sparse as sp
from scipy.ndimage import median_filter
from scipy.signal import fftconvolve
from PIL import Image

logger = logging.getLogger(__name__)

TILT_ORDER = [
    "00.50", "01.00", "01.50", "02.50",
    "04.00", "07.00", "10.00", "19.00",
]

DOWNSAMPLE = 8
BLOCK_SIZE = 32
BLOCK_STRIDE = 16
SEARCH_RANGE = 12
MIN_DATA_FRACTION = 0.05
MAX_DISP_DEG = 0.5


def compute_composite(sparse_grids: dict[str, sp.csr_matrix]) -> np.ndarray:
    """Compute 2D composite reflectivity via fmax across all tilt levels.

    Returns dense float32 array; NaN = no echo.
    """
    result = None
    for tilt in TILT_ORDER:
        sgrid = sparse_grids.get(tilt)
        if sgrid is None:
            continue
        dense = sgrid.toarray()
        dense[dense == 0] = np.nan
        if result is None:
            result = dense
        else:
            np.fmax(result, dense, out=result)

    if result is None:
        raise ValueError("No tilt grids available for composite")
    return result


def compute_motion_field(
    composite_a: np.ndarray,
    composite_b: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute displacement field between two composites via FFT block matching.

    Returns (U, V, confidence) arrays where:
        U: east-west displacement in degrees (positive = eastward)
        V: north-south displacement in degrees (positive = northward)
        confidence: 0–1 normalised cross-correlation at best match
    """
    a_ds = composite_a[::DOWNSAMPLE, ::DOWNSAMPLE].copy()
    b_ds = composite_b[::DOWNSAMPLE, ::DOWNSAMPLE].copy()

    a_ds = np.nan_to_num(a_ds, nan=0.0)
    b_ds = np.nan_to_num(b_ds, nan=0.0)

    h, w = a_ds.shape
    n_by = max(1, (h - BLOCK_SIZE) // BLOCK_STRIDE + 1)
    n_bx = max(1, (w - BLOCK_SIZE) // BLOCK_STRIDE + 1)

    u_field = np.zeros((n_by, n_bx), dtype=np.float32)
    v_field = np.zeros((n_by, n_bx), dtype=np.float32)
    conf_field = np.zeros((n_by, n_bx), dtype=np.float32)

    deg_per_ds_pixel = DOWNSAMPLE * 0.01

    for by in range(n_by):
        for bx in range(n_bx):
            y0 = by * BLOCK_STRIDE
            x0 = bx * BLOCK_STRIDE
            y1 = min(y0 + BLOCK_SIZE, h)
            x1 = min(x0 + BLOCK_SIZE, w)

            block = a_ds[y0:y1, x0:x1]
            if np.count_nonzero(block) < block.size * MIN_DATA_FRACTION:
                continue

            sy0 = max(0, y0 - SEARCH_RANGE)
            sx0 = max(0, x0 - SEARCH_RANGE)
            sy1 = min(h, y1 + SEARCH_RANGE)
            sx1 = min(w, x1 + SEARCH_RANGE)

            search = b_ds[sy0:sy1, sx0:sx1]
            if search.shape[0] <= block.shape[0] or search.shape[1] <= block.shape[1]:
                continue
            if np.count_nonzero(search) < block.size * MIN_DATA_FRACTION:
                continue

            block_zm = block - block.mean()
            search_zm = search - search.mean()

            block_norm = np.linalg.norm(block_zm)
            if block_norm < 1e-6:
                continue

            corr = fftconvolve(search_zm, block_zm[::-1, ::-1], mode="valid")
            if corr.size == 0:
                continue

            peak_idx = np.unravel_index(np.argmax(corr), corr.shape)

            ref_y = y0 - sy0
            ref_x = x0 - sx0
            dy_ds = peak_idx[0] - ref_y
            dx_ds = peak_idx[1] - ref_x

            py, px = peak_idx
            patch = search_zm[py : py + block.shape[0], px : px + block.shape[1]]
            patch_norm = np.linalg.norm(patch)
            if patch_norm > 1e-6 and patch.shape == block_zm.shape:
                ncc = corr[peak_idx] / (block_norm * patch_norm)
                conf = float(np.clip(ncc, 0.0, 1.0))
            else:
                conf = 0.0

            u_field[by, bx] = dx_ds * deg_per_ds_pixel
            v_field[by, bx] = -dy_ds * deg_per_ds_pixel
            conf_field[by, bx] = conf

    if n_by >= 3 and n_bx >= 3:
        u_field = median_filter(u_field, size=3).astype(np.float32)
        v_field = median_filter(v_field, size=3).astype(np.float32)

    return u_field, v_field, conf_field


def encode_motion_png(
    u: np.ndarray,
    v: np.ndarray,
    confidence: np.ndarray,
) -> bytes:
    """Encode (U, V, confidence) as an RGB PNG.

    R = U displacement, center 128 = no motion, range +/-MAX_DISP_DEG
    G = V displacement, center 128 = no motion
    B = confidence 0-255
    """
    r = np.clip(u / MAX_DISP_DEG * 127.5 + 128, 0, 255).astype(np.uint8)
    g = np.clip(v / MAX_DISP_DEG * 127.5 + 128, 0, 255).astype(np.uint8)
    b = np.clip(confidence * 255, 0, 255).astype(np.uint8)

    rgb = np.stack([r, g, b], axis=-1)
    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()
