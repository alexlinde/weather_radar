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
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass


# ── Lifespan: migrate legacy cache, evict stale files, seed frames ───────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading
    from . import disk_cache, pipeline
    from .cache import composite_cache, volume_cache

    disk_cache.migrate_legacy_cache()
    disk_cache.evict_older_than(hours=24)

    def _bg_seed():
        try:
            n = pipeline.seed_frames(60)
            logger.info("Seeding complete: %d volume / %d composite",
                        n, composite_cache.count())
        except Exception:
            logger.exception("Frame seeding failed")

    threading.Thread(target=_bg_seed, daemon=True, name="cache-seed").start()
    logger.info("Cache seeding started in background — server ready")

    yield


app = FastAPI(title="NYC Weather Radar API", version="4.0.0", lifespan=lifespan)

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


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/api/radar/latest")
def radar_latest():
    """Return the latest clipped NYC radar frame as JSON."""
    from .cache import composite_cache
    from . import pipeline

    entry = composite_cache.latest()
    if entry is None:
        try:
            grid, metadata = pipeline.fetch_latest_composite()
        except Exception:
            logger.exception("Failed to fetch radar data")
            raise HTTPException(status_code=502, detail="Failed to fetch radar data")
    else:
        _, frame = entry
        grid, metadata = frame.grid, frame.metadata

    rounded = np.round(grid, 2)
    data_serialisable = np.where(np.isnan(rounded), None, rounded).tolist()

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
def radar_image():
    """Return the latest NYC radar frame as a PNG with NWS colour scale."""
    from .cache import composite_cache
    from . import pipeline
    from .render import grid_to_png

    entry = composite_cache.latest()
    if entry is None:
        try:
            grid, metadata = pipeline.fetch_latest_composite()
        except Exception:
            logger.exception("Failed to fetch radar data")
            raise HTTPException(status_code=502, detail="Failed to fetch radar data")
    else:
        _, frame = entry
        grid, metadata = frame.grid, frame.metadata

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
def radar_frames(count: int = Query(default=60, ge=1, le=70)):
    """Return recent radar frames as base64-encoded PNGs, oldest-first."""
    from .cache import composite_cache
    from .render import grid_to_png

    frames_data = composite_cache.items(count)
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


@app.get("/api/radar/volume")
async def radar_volume():
    """Return the latest multi-level radar snapshot as voxel data."""
    try:
        from . import pipeline

        volume = await asyncio.to_thread(pipeline.fetch_volume_snapshot)
    except Exception:
        logger.exception("Failed to fetch volume data")
        raise HTTPException(status_code=502, detail="Failed to fetch volume data")

    return {
        "timestamp": volume["timestamp"],
        "bounds": volume["bounds"],
        "voxels": volume["voxels"],
    }


@app.get("/api/radar/volume/frames")
async def radar_volume_frames(count: int = Query(default=60, ge=1, le=70)):
    """Return recent volume frames as pre-computed voxel data, oldest-first."""
    from . import pipeline

    frames = pipeline.get_volume_frames(count)
    if not frames:
        raise HTTPException(
            status_code=503,
            detail="Volume frames not yet cached — server is still seeding",
        )
    return {"frames": frames, "count": len(frames)}


@app.get("/api/radar/tiles/{timestamp}/{z}/{x}/{y}.png")
def radar_tile(timestamp: str, z: int, x: int, y: int):
    """Serve a single 256x256 radar tile for the given timestamp and tile coordinates."""
    from .cache import conus_cache
    from .tiles import MAX_ZOOM, MIN_ZOOM, render_tile, tile_cache

    if z < MIN_ZOOM or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail=f"Zoom must be {MIN_ZOOM}–{MAX_ZOOM}")

    cache_key = (timestamp, z, x, y)
    cached = tile_cache.get(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600, immutable"},
        )

    result = conus_cache.get(timestamp)
    if result is None:
        raise HTTPException(status_code=404, detail="Timestamp not available")
    grid, meta = result

    png_bytes = render_tile(grid, meta, z, x, y)
    tile_cache.put(cache_key, png_bytes)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600, immutable"},
    )


@app.get("/api/radar/timestamps")
def radar_timestamps():
    """Return the list of available radar timestamps (for tile URL construction)."""
    from . import disk_cache

    composites = disk_cache.list_composites()
    if not composites:
        raise HTTPException(
            status_code=503,
            detail="No composites cached yet — server is still seeding",
        )
    return {"timestamps": composites, "count": len(composites)}


@app.get("/api/radar/refresh")
async def radar_refresh():
    """Force cache invalidation and re-seed both caches."""
    from . import pipeline

    pipeline.invalidate_all()
    count = await asyncio.to_thread(pipeline.seed_frames, 60)
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
    from .cache import composite_cache, volume_cache

    return {
        "status": "ok",
        "cached_frames": composite_cache.count(),
        "volume_frames": volume_cache.count(),
    }
