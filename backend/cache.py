"""
Simple in-memory frame cache with TTL.

MRMS updates every ~2 minutes, so we cache for 120 seconds to avoid
hammering S3 on every request.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

TTL_SECONDS = 120


class _FrameCache:
    def __init__(self, ttl: int = TTL_SECONDS) -> None:
        self._ttl = ttl
        self._grid: np.ndarray | None = None
        self._metadata: dict | None = None
        self._fetched_at: float = 0.0

    def _is_fresh(self) -> bool:
        return (
            self._grid is not None
            and (time.monotonic() - self._fetched_at) < self._ttl
        )

    def get_or_fetch(self) -> tuple[np.ndarray, dict]:
        """Return the cached frame if fresh; otherwise fetch a new one."""
        if self._is_fresh():
            age = time.monotonic() - self._fetched_at
            logger.debug("Cache hit (age %.1fs)", age)
            return self._grid, self._metadata  # type: ignore[return-value]

        logger.info("Cache miss — fetching latest MRMS frame…")
        from .mrms import get_latest_frame

        grid, metadata = get_latest_frame()
        self._grid = grid
        self._metadata = metadata
        self._fetched_at = time.monotonic()
        logger.info("Cache updated: %s", metadata.get("timestamp"))
        return grid, metadata

    def invalidate(self) -> None:
        """Force the next call to re-fetch."""
        self._fetched_at = 0.0


# Module-level singleton
_cache = _FrameCache()


def get_or_fetch_latest() -> tuple[np.ndarray, dict]:
    """Return the latest (possibly cached) radar frame and metadata."""
    return _cache.get_or_fetch()


def invalidate() -> None:
    """Invalidate the cache, forcing the next request to re-fetch."""
    _cache.invalidate()
