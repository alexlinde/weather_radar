"""
Unified in-memory cache for CONUS tilt grids (scipy.sparse CSR).

Single ConusTiltCache serves both 2D composite tiles and 3D voxel tiles.
Composites are derived lazily via np.fmax across tilt grids.
Falls back to disk_cache on LRU miss (~33ms load from sparse .npz).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

import numpy as np
import scipy.sparse as sp

logger = logging.getLogger(__name__)


class ConusTiltCache:
    """Thread-safe LRU for sparse tilt grid sets, with lazy composite derivation.

    Each entry holds 8 sparse CSR tilt grids (~39 MB) + metadata.
    Composites (~98 MB dense) are derived on first 2D tile request and cached.
    """

    def __init__(self, max_size: int = 20) -> None:
        self._max = max_size
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, timestamp: str) -> dict[str, Any] | None:
        """Get a cache entry, loading from disk on miss.

        Returns dict with keys: 'grids' (sparse), 'meta', and optionally 'composite' (dense).
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
        entry = {"grids": grids, "meta": meta, "composite": None}

        with self._lock:
            if timestamp in self._data:
                self._data.move_to_end(timestamp)
                return self._data[timestamp]
            if len(self._data) >= self._max:
                self._data.popitem(last=False)
            self._data[timestamp] = entry
        return entry

    def get_composite(self, timestamp: str) -> tuple[np.ndarray, dict] | None:
        """Get the 2D composite for a timestamp, computing lazily from tilt grids."""
        entry = self.get(timestamp)
        if entry is None:
            return None

        if entry["composite"] is not None:
            return entry["composite"], entry["meta"]

        grids = entry["grids"]
        if not grids:
            return None

        tilt_keys = sorted(grids.keys())
        composite = grids[tilt_keys[0]].toarray().astype(np.float32)
        for tilt in tilt_keys[1:]:
            dense = grids[tilt].toarray().astype(np.float32)
            np.fmax(composite, dense, out=composite)

        composite[composite == 0] = np.nan
        entry["composite"] = composite
        return composite, entry["meta"]

    def put(self, timestamp: str, grids: dict[str, sp.csr_matrix], meta: dict) -> None:
        """Insert a tilt grid set directly (used during seeding)."""
        entry = {"grids": grids, "meta": meta, "composite": None}
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
