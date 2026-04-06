"""
In-memory frame caches for decoded radar data.

Two BoundedCache instances:
  composite_cache — 2D composite grids keyed by S3 ref_key
  volume_cache    — 3D voxel lists keyed by ISO timestamp

Both are passive stores. The pipeline module is responsible for
populating them; these caches never trigger network requests.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import numpy as np

logger = logging.getLogger(__name__)

V = TypeVar("V")
MAX_FRAMES = 70


@dataclass(slots=True)
class FrameEntry:
    metadata: dict[str, Any]
    grid: np.ndarray


class BoundedCache(Generic[V]):
    """Thread-safe ordered dict with bounded size and oldest-first eviction."""

    def __init__(self, max_size: int = MAX_FRAMES) -> None:
        self._max = max_size
        self._data: OrderedDict[str, V] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> V | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: V) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return
            if len(self._data) >= self._max:
                self._data.popitem(last=False)
            self._data[key] = value

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def latest(self) -> tuple[str, V] | None:
        """Return the (key, value) of the most recently inserted entry."""
        with self._lock:
            if not self._data:
                return None
            key = next(reversed(self._data))
            return key, self._data[key]

    def items(self, count: int) -> list[tuple[str, V]]:
        """Return up to `count` entries, oldest-first."""
        with self._lock:
            all_items = list(self._data.items())
        return all_items[-count:] if count < len(all_items) else all_items

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class ConusCompositeCache:
    """LRU cache for loaded CONUS composite grids (backed by disk via disk_cache).

    Holds a small number of full-CONUS numpy arrays in memory. On miss,
    falls back to ``disk_cache.get_composite()`` and promotes the result.
    """

    def __init__(self, max_size: int = 5) -> None:
        self._max = max_size
        self._data: OrderedDict[str, tuple[np.ndarray, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, timestamp: str) -> tuple[np.ndarray, dict] | None:
        with self._lock:
            if timestamp in self._data:
                self._data.move_to_end(timestamp)
                return self._data[timestamp]

        from . import disk_cache
        result = disk_cache.get_composite(timestamp)
        if result is None:
            return None

        with self._lock:
            if timestamp in self._data:
                self._data.move_to_end(timestamp)
                return self._data[timestamp]
            if len(self._data) >= self._max:
                self._data.popitem(last=False)
            self._data[timestamp] = result
        return result

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# ── Module-level instances ────────────────────────────────────────────────────

composite_cache: BoundedCache[FrameEntry] = BoundedCache(MAX_FRAMES)
volume_cache: BoundedCache[list] = BoundedCache(MAX_FRAMES)
conus_cache = ConusCompositeCache()
