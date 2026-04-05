"""
FastAPI backend for the NYC Weather Radar prototype.

Endpoints:
    GET /api/radar/latest   — latest clipped radar data as JSON
    GET /api/radar/image    — latest clipped radar rendered as PNG (NWS color scale)
    GET /api/radar/frames   — batch of recent frames as base64 PNGs for animation
    GET /api/radar/refresh  — force cache invalidation and immediate re-fetch
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass


# ── Lifespan: seed frame cache on startup ─────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import disk_cache, mrms

    disk_cache.evict_older_than(hours=24)
    logger.info("Seeding frame cache with latest 60 frames (~2 hours)…")
    count = await asyncio.to_thread(mrms.get_recent_frames, 60)
    logger.info("Cache seeded with %d frames — ready to serve", count)
    yield


app = FastAPI(title="NYC Weather Radar API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
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


def grid_to_png(grid: np.ndarray) -> bytes:
    """Convert a 2D dBZ array to a PNG with NWS color scale. Transparent below 5 dBZ."""
    rows, cols = grid.shape
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)

    for min_dbz, max_dbz, r, g, b in NWS_DBZ_COLORS:
        mask = (~np.isnan(grid)) & (grid >= min_dbz) & (grid < max_dbz)
        rgba[mask] = [r, g, b, 220]

    rgba[(~np.isnan(grid)) & (grid >= 65)] = [200, 200, 255, 220]

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/api/radar/latest")
async def radar_latest():
    """Return the latest clipped NYC radar frame as JSON."""
    try:
        from .cache import get_or_fetch_latest

        grid, metadata = get_or_fetch_latest()
    except Exception as exc:
        logger.exception("Failed to fetch radar data")
        raise HTTPException(status_code=502, detail=str(exc))

    data_serialisable = [
        [None if np.isnan(v) else round(float(v), 2) for v in row] for row in grid
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
    """Return the latest NYC radar frame as a PNG with NWS colour scale."""
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


@app.get("/api/radar/frames")
async def radar_frames(count: int = Query(default=60, ge=1, le=70)):
    """
    Return up to `count` recent radar frames as base64-encoded PNGs.
    Frames are ordered oldest-first (chronological) for animation playback.
    """
    from . import cache

    frames_data = cache.get_frames(count)
    if not frames_data:
        raise HTTPException(status_code=503, detail="No frames cached yet")

    result = []
    for _key, entry in frames_data:
        png_bytes = grid_to_png(entry.grid)
        b64 = base64.b64encode(png_bytes).decode("ascii")
        meta = entry.metadata
        result.append(
            {
                "timestamp": meta["timestamp"],
                "image": f"data:image/png;base64,{b64}",
                "bounds": {
                    "north": meta["north"],
                    "south": meta["south"],
                    "east": meta["east"],
                    "west": meta["west"],
                },
            }
        )

    return {"frames": result, "count": len(result)}


@app.get("/api/radar/refresh")
async def radar_refresh():
    """Force cache invalidation and re-seed."""
    from . import cache, mrms

    cache.invalidate()
    count = await asyncio.to_thread(mrms.get_recent_frames, 60)
    return {"status": "refreshed", "frames": count}


@app.get("/api/config")
async def api_config():
    """Return frontend configuration (map tile API keys, if present)."""
    return {
        "stadia_api_key": os.getenv("STADIA_API_KEY", ""),
        "maptiler_api_key": os.getenv("MAPTILER_API_KEY", ""),
    }


@app.get("/health")
async def health():
    from . import cache

    return {"status": "ok", "cached_frames": cache.frame_count()}
