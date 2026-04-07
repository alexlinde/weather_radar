"""
FastAPI backend for the Weather Radar viewer.

Endpoints:
    GET /api/radar/atlas/{timestamp}/{z}/{x}/{y}.png — atlas tile (8 tilts, grayscale PNG)
    GET /api/radar/motion/{timestamp}.png            — motion vector field (RGB PNG)
    GET /api/radar/timestamps                        — available timestamps
    GET /api/radar/refresh                           — force re-seed
    GET /api/config                                  — frontend map config
    GET /health                                      — cache status
"""

from __future__ import annotations

import asyncio
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


DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")


REFRESH_INTERVAL_S = 60
PURGE_MAX_AGE_H = 3.0
PURGE_MAX_FRAMES = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading
    from . import disk_cache, pipeline

    disk_cache.migrate_legacy_cache()
    disk_cache.evict_older_than(hours=24)

    _refresh_stop = asyncio.Event()

    async def _periodic_refresh():
        """Check S3 for new frames and purge stale data every REFRESH_INTERVAL_S."""
        while not _refresh_stop.is_set():
            try:
                await asyncio.sleep(REFRESH_INTERVAL_S)
            except asyncio.CancelledError:
                break
            if _refresh_stop.is_set():
                break
            try:
                fetched = await asyncio.to_thread(pipeline.refresh_new_frames, 10)
                purged = await asyncio.to_thread(
                    pipeline.purge_stale_data, PURGE_MAX_AGE_H, PURGE_MAX_FRAMES,
                )
                if fetched or purged:
                    logger.info(
                        "Periodic refresh: %d new frames, %d stale entries purged",
                        fetched, purged,
                    )
            except Exception:
                logger.exception("Periodic refresh failed")

    if DEV_MODE:
        n = pipeline.warm_from_disk(limit=30)
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

    refresh_task = asyncio.create_task(_periodic_refresh())
    logger.info("Periodic refresh task started (every %ds)", REFRESH_INTERVAL_S)

    yield

    _refresh_stop.set()
    refresh_task.cancel()
    try:
        await refresh_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Weather Radar API", version="5.0.0", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Atlas tiles (8 tilts packed into one grayscale PNG) ───────────────────────


MAX_FRAMES = 30


@app.get("/api/radar/atlas/{timestamp}/{z}/{x}/{y}.png")
def radar_atlas_tile(timestamp: str, z: int, x: int, y: int):
    """Serve a 256×2048 grayscale PNG atlas tile with 8 tilt bands."""
    from .cache import tilt_cache
    from .tiles import (
        MAX_ZOOM, MIN_ZOOM,
        atlas_tile_cache, render_atlas_tile,
    )

    if z < MIN_ZOOM or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail=f"Zoom must be {MIN_ZOOM}–{MAX_ZOOM}")

    tilt_entry = tilt_cache.get(timestamp)
    if tilt_entry is None:
        raise HTTPException(status_code=404, detail="Timestamp not found")

    cache_key = (timestamp, z, x, y)
    cached = atlas_tile_cache.get(cache_key)
    if cached is None:
        cached = render_atlas_tile(
            tilt_entry["grids"], tilt_entry["meta"], z, x, y,
        )
        atlas_tile_cache.put(cache_key, cached)

    return Response(
        content=cached,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600, immutable"},
    )


# ── Motion vector tiles ──────────────────────────────────────────────────────


@app.get("/api/radar/motion/{timestamp}.png")
def radar_motion_tile(timestamp: str):
    """Serve the motion vector field for *timestamp* → next frame as RGB PNG."""
    from . import disk_cache

    png = disk_cache.get_motion_png(timestamp)
    if png is None:
        raise HTTPException(status_code=404, detail="Motion data not available")

    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600, immutable"},
    )


# ── Timestamps ───────────────────────────────────────────────────────────────


@app.get("/api/radar/timestamps")
def radar_timestamps():
    """Return the list of available radar timestamps with motion availability."""
    from . import disk_cache
    from .motion import MAX_DISP_DEG

    entries = disk_cache.list_tilt_grid_timestamps()
    if not entries:
        raise HTTPException(
            status_code=503,
            detail="No frames cached yet — server is still seeding",
        )
    entries = entries[-MAX_FRAMES:]
    return {
        "timestamps": entries,
        "count": len(entries),
        "motion": {"max_disp_deg": MAX_DISP_DEG},
    }


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


@app.get("/ready")
async def ready():
    """Readiness probe: returns 200 once frames are available, 503 during seeding."""
    from . import disk_cache

    entries = disk_cache.list_tilt_grid_timestamps()
    if not entries:
        raise HTTPException(status_code=503, detail="Seeding in progress")
    return {"status": "ready", "frames": len(entries)}


# ── Serve frontend static files ──────────────────────────────────────────────

_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_SRC_DIR = Path(__file__).resolve().parent.parent / "frontend"
_FRONTEND_DIR = _DIST_DIR if _DIST_DIR.is_dir() else _SRC_DIR
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
