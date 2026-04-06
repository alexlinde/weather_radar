"""
In-memory LRU cache for CONUS tilt grids (scipy.sparse CSR).

Voxel tiles are derived on-demand from the sparse grids stored here.
Falls back to disk_cache on LRU miss (~33ms load from sparse .npz).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

import scipy.sparse as sp

logger = logging.getLogger(__name__)


class ConusTiltCache:
    """Thread-safe LRU for sparse tilt grid sets.

    Each entry holds 8 sparse CSR tilt grids (~39 MB) + metadata.
    """

    def __init__(self, max_size: int = 20) -> None:
        self._max = max_size
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, timestamp: str) -> dict[str, Any] | None:
        """Get a cache entry, loading from disk on miss.

        Returns dict with keys: 'grids' (sparse) and 'meta'.
        """
        with self._lock:
            if timestamp in self._data:
                self._data.move_to_end(timestamp)
                return self._data[timestamp]

        from . import disk_cache
        result = disk_cache.get_tilt_grids(timestamp)
        if result is None:
            return None

        grids, meta = result
        entry = {"grids": grids, "meta": meta}

        with self._lock:
            if timestamp in self._data:
                self._data.move_to_end(timestamp)
                return self._data[timestamp]
            if len(self._data) >= self._max:
                self._data.popitem(last=False)
            self._data[timestamp] = entry
        return entry

    def put(self, timestamp: str, grids: dict[str, sp.csr_matrix], meta: dict) -> None:
        """Insert a tilt grid set directly (used during seeding)."""
        entry = {"grids": grids, "meta": meta}
        with self._lock:
            if timestamp in self._data:
                self._data.move_to_end(timestamp)
                return
            if len(self._data) >= self._max:
                self._data.popitem(last=False)
            self._data[timestamp] = entry

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# Module-level singleton
tilt_cache = ConusTiltCache()
