"""
FastAPI backend for the Weather Radar viewer.

Endpoints:
    GET /api/radar/tiles/{ts}/{z}/{x}/{y}.png        — 2D composite tile
    GET /api/radar/volume/tiles/{ts}/{z}/{x}/{y}.json — 3D voxel tile
    GET /api/radar/timestamps                        — available timestamps
    GET /api/radar/refresh                           — force re-seed
    GET /api/config                                  — frontend map config
    GET /health                                      — cache status
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

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

    disk_cache.migrate_legacy_cache()
    disk_cache.evict_older_than(hours=24)

    def _bg_seed():
        try:
            n = pipeline.seed_frames(60)
            logger.info("Seeding complete: %d timestamps cached", n)
        except Exception:
            logger.exception("Frame seeding failed")

    threading.Thread(target=_bg_seed, daemon=True, name="cache-seed").start()
    logger.info("Cache seeding started in background — server ready")

    yield


app = FastAPI(title="Weather Radar API", version="5.0.0", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── 2D composite tiles ──────────────────────────────────────────────────────


@app.get("/api/radar/tiles/{timestamp}/{z}/{x}/{y}.png")
def radar_tile(timestamp: str, z: int, x: int, y: int):
    """Serve a single 256x256 radar composite tile."""
    from .cache import tilt_cache
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

    result = tilt_cache.get_composite(timestamp)
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


# ── 3D voxel tiles ──────────────────────────────────────────────────────────


@app.get("/api/radar/volume/tiles/{timestamp}/{z}/{x}/{y}.json")
def radar_voxel_tile(timestamp: str, z: int, x: int, y: int):
    """Serve voxel data for a tile region across all tilt levels."""
    from .cache import tilt_cache
    from .tiles import MAX_ZOOM, MIN_ZOOM, render_voxel_tile, voxel_tile_cache

    if z < MIN_ZOOM or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail=f"Zoom must be {MIN_ZOOM}–{MAX_ZOOM}")

    cache_key = (timestamp, z, x, y)
    cached = voxel_tile_cache.get(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=3600, immutable"},
        )

    entry = tilt_cache.get(timestamp)
    if entry is None:
        raise HTTPException(status_code=404, detail="Timestamp not available")

    voxels = render_voxel_tile(entry["grids"], entry["meta"], z, x, y)
    payload = json.dumps({"voxels": voxels, "count": len(voxels)})
    payload_bytes = payload.encode()
    voxel_tile_cache.put(cache_key, payload_bytes)

    return Response(
        content=payload_bytes,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600, immutable"},
    )


# ── 3D binary voxel tiles ────────────────────────────────────────────────────


@app.get("/api/radar/volume/tiles/{timestamp}/{z}/{x}/{y}.bin")
def radar_voxel_tile_bin(
    timestamp: str, z: int, x: int, y: int,
    base: str | None = None,
):
    """Serve voxel data as a compact binary tile.

    Binary format: [uint32 count][float32 positions (3×N)][uint8 colors (4×N)].
    With ?base=<prev_timestamp>, returns 4-byte unchanged sentinel (0xFFFFFFFF)
    if the tile content is identical to the base frame.
    """
    from .cache import tilt_cache
    from .tiles import (
        DELTA_UNCHANGED, MAX_ZOOM, MIN_ZOOM,
        bin_tile_cache, render_voxel_tile_binary,
    )

    if z < MIN_ZOOM or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail=f"Zoom must be {MIN_ZOOM}–{MAX_ZOOM}")

    cache_key = (timestamp, z, x, y)
    cached = bin_tile_cache.get(cache_key)

    if cached is None:
        entry = tilt_cache.get(timestamp)
        if entry is None:
            raise HTTPException(status_code=404, detail="Timestamp not available")
        cached = render_voxel_tile_binary(entry["grids"], entry["meta"], z, x, y)
        bin_tile_cache.put(cache_key, cached)

    if base:
        base_key = (base, z, x, y)
        base_cached = bin_tile_cache.get(base_key)
        if base_cached is not None and base_cached == cached:
            return Response(
                content=DELTA_UNCHANGED,
                media_type="application/octet-stream",
                headers={"Cache-Control": "public, max-age=3600, immutable"},
            )

    return Response(
        content=cached,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600, immutable"},
    )


# ── Timestamps ───────────────────────────────────────────────────────────────


@app.get("/api/radar/timestamps")
def radar_timestamps():
    """Return the list of available radar timestamps."""
    from . import disk_cache

    entries = disk_cache.list_tilt_grid_timestamps()
    if not entries:
        raise HTTPException(
            status_code=503,
            detail="No frames cached yet — server is still seeding",
        )
    return {"timestamps": entries, "count": len(entries)}


# ── Admin / config ───────────────────────────────────────────────────────────


@app.get("/api/radar/refresh")
async def radar_refresh():
    """Force cache invalidation and re-seed."""
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
    from .cache import tilt_cache
    from . import disk_cache

    return {
        "status": "ok",
        "cached_in_memory": tilt_cache.count(),
        "cached_on_disk": len(disk_cache.list_tilt_grid_timestamps()),
    }


# ── Serve frontend static files ──────────────────────────────────────────────

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
