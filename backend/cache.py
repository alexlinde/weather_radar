"""
Multi-frame in-memory cache for decoded radar data.

Stores up to MAX_FRAMES recent frames keyed by S3 object key.
The S3 key encodes the timestamp, so it's a natural dedup key.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MAX_FRAMES = 70


@dataclass(slots=True)
class FrameEntry:
    metadata: dict[str, Any]
    grid: np.ndarray


class FrameCache:
    def __init__(self, max_frames: int = MAX_FRAMES) -> None:
        self._max = max_frames
        self._frames: OrderedDict[str, FrameEntry] = OrderedDict()
        self._lock = threading.Lock()

    def has(self, s3_key: str) -> bool:
        with self._lock:
            return s3_key in self._frames

    def get(self, s3_key: str) -> FrameEntry | None:
        with self._lock:
            return self._frames.get(s3_key)

    def store(self, s3_key: str, metadata: dict, grid: np.ndarray) -> None:
        """Add a decoded frame. Evicts the oldest if at capacity."""
        with self._lock:
            if s3_key in self._frames:
                self._frames.move_to_end(s3_key)
                return
            if len(self._frames) >= self._max:
                evicted_key, _ = self._frames.popitem(last=False)
                logger.debug("Evicted oldest frame: %s", evicted_key)
            self._frames[s3_key] = FrameEntry(metadata=metadata, grid=grid)

    def get_latest(self) -> tuple[np.ndarray, dict] | None:
        """Return the most recently inserted frame, or None."""
        with self._lock:
            if not self._frames:
                return None
            entry = next(reversed(self._frames.values()))
            return entry.grid, entry.metadata

    def get_frames(self, count: int) -> list[tuple[str, FrameEntry]]:
        """
        Return up to `count` most recent frames as (s3_key, entry) pairs,
        ordered oldest-first (chronological) for animation playback.
        """
        with self._lock:
            items = list(self._frames.items())
        recent = items[-count:] if count < len(items) else items
        return recent

    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def invalidate(self) -> None:
        """Clear all cached frames."""
        with self._lock:
            self._frames.clear()


_cache = FrameCache()


def get_or_fetch_latest() -> tuple[np.ndarray, dict]:
    """Return the latest radar frame, fetching if cache is empty."""
    result = _cache.get_latest()
    if result is not None:
        return result

    logger.info("Cache empty — fetching latest MRMS frame…")
    from .mrms import fetch_and_decode, list_latest_files

    keys = list_latest_files(count=1)
    if not keys:
        raise RuntimeError("No MRMS files found in S3 bucket")
    grid, metadata = fetch_and_decode(keys[0])
    _cache.store(keys[0], metadata, grid)
    return grid, metadata


def store(s3_key: str, metadata: dict, grid: np.ndarray) -> None:
    _cache.store(s3_key, metadata, grid)


def get(s3_key: str) -> FrameEntry | None:
    return _cache.get(s3_key)


def has(s3_key: str) -> bool:
    return _cache.has(s3_key)


def get_frames(count: int = 10) -> list[tuple[str, FrameEntry]]:
    return _cache.get_frames(count)


def frame_count() -> int:
    return _cache.frame_count()


def invalidate() -> None:
    _cache.invalidate()
