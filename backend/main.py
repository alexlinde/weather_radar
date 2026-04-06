"""
FastAPI backend for the Weather Radar viewer.

Endpoints:
    GET /api/radar/volume/bulk/{z}/{x}/{y}.bin  — all frames' voxels for a tile
    GET /api/radar/timestamps                   — available timestamps
    GET /api/radar/refresh                      — force re-seed
    GET /api/config                             — frontend map config
    GET /health                                 — cache status
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
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


DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading
    from . import disk_cache, pipeline

    disk_cache.migrate_legacy_cache()
    disk_cache.evict_older_than(hours=24)

    if DEV_MODE:
        n = pipeline.warm_from_disk(limit=20)
        logger.info("DEV_MODE: warmed %d frames from disk cache (no S3 fetch)", n)
    else:
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


# ── Bulk voxel tiles (all frames in one response) ────────────────────────────


MAX_FRAMES = 20


@app.get("/api/radar/volume/bulk/{z}/{x}/{y}.bin")
def radar_voxel_bulk(z: int, x: int, y: int):
    """Serve ALL frames' voxels for a tile coordinate in one binary response.

    Format: [uint16 frame_count][per-frame: existing binary tile format]
    Each frame block is [uint32 count][float32 positions[3*N]][uint8 colors[4*N]].
    GZip middleware compresses the response (~80-90% reduction).
    """
    from .cache import tilt_cache
    from .tiles import (
        MAX_ZOOM, MIN_ZOOM,
        bin_tile_cache, render_voxel_tile_binary,
    )
    from . import disk_cache

    if z < MIN_ZOOM or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail=f"Zoom must be {MIN_ZOOM}–{MAX_ZOOM}")

    entries = disk_cache.list_tilt_grid_timestamps()
    entries = entries[-MAX_FRAMES:]

    if not entries:
        raise HTTPException(
            status_code=503,
            detail="No frames cached yet — server is still seeding",
        )

    frame_chunks: list[bytes] = []
    for entry in entries:
        ts = entry["timestamp"]
        cache_key = (ts, z, x, y)
        cached = bin_tile_cache.get(cache_key)

        if cached is None:
            tilt_entry = tilt_cache.get(ts)
            if tilt_entry is None:
                cached = struct.pack("<I", 0)
            else:
                cached = render_voxel_tile_binary(
                    tilt_entry["grids"], tilt_entry["meta"], z, x, y,
                )
                bin_tile_cache.put(cache_key, cached)

        frame_chunks.append(cached)

    header = struct.pack("<H", len(frame_chunks))
    return Response(
        content=header + b"".join(frame_chunks),
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=120"},
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
    entries = entries[-MAX_FRAMES:]
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
