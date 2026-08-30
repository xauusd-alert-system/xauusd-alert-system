"""TTL cache for symbol_info / symbol_info_tick reads (ТЗ 8.6).

The MT5 terminal is the source of truth, but ``symbol_info`` (contract specs)
changes at most on terminal config edits and even ``symbol_info_tick`` is
quoted at most once per terminal poll. A short TTL cache reduces the call
volume through the rate limiter (P2-7) without introducing stale values into
the trading path: fresh reads (``MT5Client.symbol_info_tick``) bypass the
cache; only ``symbol_info_tick_cached`` / ``symbol_info_cached`` use it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable


class SymbolCache:
    """TTL cache with lazy per-key fetch via ``get_or_fetch``.

    Args:
        ttl_ms: time-to-live per entry in milliseconds (default 500).
        clock: monotonic time provider returning seconds (injectable for
            tests).
    """

    def __init__(self, ttl_ms: int | float = 500, clock=time.monotonic):
        if ttl_ms < 0:
            raise ValueError("ttl_ms must be >= 0")
        self.ttl_ms = float(ttl_ms)
        self.clock = clock
        self._store: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        # Statistics (useful for tests and call-count assertions).
        self.hits = 0
        self.misses = 0

    @property
    def ttl_seconds(self) -> float:
        return self.ttl_ms / 1000.0

    def get(self, key: Hashable) -> tuple[bool, Any]:
        """Return ``(hit, value)``; expired entries are treated as misses."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            stored_at, value = entry
            if (self.clock() - stored_at) > self.ttl_seconds:
                # lazily drop expired entry
                self._store.pop(key, None)
                return False, None
            return True, value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._store[key] = (self.clock(), value)

    def invalidate(self, key: Hashable | None = None) -> None:
        """Drop one key or the whole cache (``key=None``)."""
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)

    def get_or_fetch(self, key: Hashable, fetcher: Callable[[], Any]) -> Any:
        """Return the cached value when fresh, else call ``fetcher()``, cache
        and return its result.

        ``None`` results from the fetcher are NOT cached (a failed read must
        not pin a "no data" answer for the whole TTL)."""
        hit, value = self.get(key)
        if hit:
            self.hits += 1
            return value
        self.misses += 1
        value = fetcher()
        if value is not None:
            self.set(key, value)
        return value

    def size(self) -> int:
        with self._lock:
            return len(self._store)
