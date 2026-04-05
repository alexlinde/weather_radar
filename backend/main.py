"""
FastAPI backend for the NYC Weather Radar prototype.

Endpoints:
    GET /api/radar/latest   — latest clipped radar data as JSON
    GET /api/radar/image    — latest clipped radar rendered as PNG (NWS color scale)
    GET /api/radar/refresh  — force cache invalidation and immediate re-fetch
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Load .env if present (for API keys used by the frontend)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

app = FastAPI(title="NYC Weather Radar API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
    # Custom headers must be explicitly exposed or the browser silently gets null
    expose_headers=[
        "X-Radar-Timestamp",
        "X-Radar-North",
        "X-Radar-South",
        "X-Radar-West",
        "X-Radar-East",
    ],
)

# ── NWS Reflectivity Color Scale ──────────────────────────────────────────────

NWS_DBZ_COLORS: list[tuple[float, float, int, int, int]] = [
    (5,   10,  64,  192, 64),
    (10,  15,  48,  160, 48),
    (15,  20,  0,   144, 0),
    (20,  25,  0,   120, 0),
    (25,  30,  255, 255, 0),
    (30,  35,  230, 180, 0),
    (35,  40,  255, 100, 0),
    (40,  45,  255, 0,   0),
    (45,  50,  200, 0,   0),
    (50,  55,  180, 0,   120),
    (55,  60,  150, 0,   200),
    (60,  65,  255, 255, 255),
    (65,  75,  200, 200, 255),
]


def dbz_to_rgba(dbz_value: float) -> tuple[int, int, int, int]:
    """Map a dBZ value to an RGBA tuple. Returns (0,0,0,0) for < 5 dBZ or NaN."""
    if np.isnan(dbz_value) or dbz_value < 5.0:
        return (0, 0, 0, 0)
    for min_dbz, max_dbz, r, g, b in NWS_DBZ_COLORS:
        if min_dbz <= dbz_value < max_dbz:
            return (r, g, b, 220)
    # Above highest threshold → use the last colour
    return (200, 200, 255, 220)


def grid_to_png(grid: np.ndarray) -> bytes:
    """
    Convert a 2D dBZ array to a PNG image using the NWS color scale.
    Transparent where dBZ < 5 or NaN.
    """
    rows, cols = grid.shape
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)

    for min_dbz, max_dbz, r, g, b in NWS_DBZ_COLORS:
        mask = (~np.isnan(grid)) & (grid >= min_dbz) & (grid < max_dbz)
        rgba[mask] = [r, g, b, 220]

    # Values above the highest band
    rgba[(~np.isnan(grid)) & (grid >= 65)] = [200, 200, 255, 220]

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    logger.info("NYC Weather Radar API starting up. Cache will be populated on first request.")


@app.get("/api/radar/latest")
async def radar_latest():
    """
    Return the latest clipped NYC radar frame as JSON.

    Response shape:
    {
        "timestamp": "2026-04-05T22:42:38Z",
        "bounds": { "north": 41.0, "south": 40.4, "east": -73.6, "west": -74.3 },
        "grid": { "rows": 61, "cols": 71 },
        "data": [[...]]   -- 2D array of dBZ values; null for missing
    }
    """
    try:
        from .cache import get_or_fetch_latest
        grid, metadata = get_or_fetch_latest()
    except Exception as exc:
        logger.exception("Failed to fetch radar data")
        raise HTTPException(status_code=502, detail=str(exc))

    # Replace NaN with None for JSON serialisation
    data_serialisable = [
        [None if np.isnan(v) else round(float(v), 2) for v in row]
        for row in grid
    ]

    return {
        "timestamp": metadata["timestamp"],
        "bounds": {
            "north": metadata["north"],
            "south": metadata["south"],
            "east": metadata["east"],
            "west": metadata["west"],
        },
        "grid": {
            "rows": int(metadata["Nj"]),
            "cols": int(metadata["Ni"]),
        },
        "data": data_serialisable,
    }


@app.get("/api/radar/image")
async def radar_image():
    """
    Return the latest NYC radar frame as a PNG with NWS colour scale applied.

    Radar bounds are embedded in the X-Radar-* response headers for the frontend
    to correctly position the image overlay.
    """
    try:
        from .cache import get_or_fetch_latest
        grid, metadata = get_or_fetch_latest()
    except Exception as exc:
        logger.exception("Failed to fetch radar data")
        raise HTTPException(status_code=502, detail=str(exc))

    png_bytes = grid_to_png(grid)

    headers = {
        "X-Radar-Timestamp": metadata["timestamp"] or "",
        "X-Radar-North": str(metadata["north"]),
        "X-Radar-South": str(metadata["south"]),
        "X-Radar-West": str(metadata["west"]),
        "X-Radar-East": str(metadata["east"]),
        "Cache-Control": "max-age=60",
    }

    return Response(content=png_bytes, media_type="image/png", headers=headers)


@app.get("/api/radar/refresh")
async def radar_refresh():
    """Force cache invalidation. The next request will re-fetch from S3."""
    from .cache import invalidate
    invalidate()
    return {"status": "cache invalidated"}


@app.get("/api/config")
async def api_config():
    """Return frontend configuration (map tile API keys, if present)."""
    return {
        "stadia_api_key": os.getenv("STADIA_API_KEY", ""),
        "maptiler_api_key": os.getenv("MAPTILER_API_KEY", ""),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
