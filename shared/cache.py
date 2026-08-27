"""In-memory TTL Cache with expiration and eviction (P2-10)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    """Thread-safe-friendly in-memory TTL Cache."""

    def __init__(
        self,
        default_ttl_seconds: float = 60.0,
        maxsize: int = 1000,
        clock: Callable[[], float] = time.time,
    ):
        self.default_ttl = default_ttl_seconds
        self.maxsize = maxsize
        self.clock = clock
        self._store: Dict[str, _CacheEntry] = {}

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return default
        now = self.clock()
        if now >= entry.expires_at:
            del self._store[key]
            return default
        return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        if len(self._store) >= self.maxsize:
            self._evict_expired()
            if len(self._store) >= self.maxsize:
                # Evict oldest entry
                first_key = next(iter(self._store))
                del self._store[first_key]

        effective_ttl = ttl if ttl is not None else self.default_ttl
        expires_at = self.clock() + effective_ttl
        self._store[key] = _CacheEntry(value=value, expires_at=expires_at)

    def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    def clear(self) -> None:
        self._store.clear()

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def _evict_expired(self) -> None:
        now = self.clock()
        expired = [k for k, v in self._store.items() if now >= v.expires_at]
        for k in expired:
            del self._store[k]

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._store)


def cached(cache: TTLCache, ttl_seconds: Optional[float] = None):
    """Decorator to cache function results in a TTLCache."""
    import functools

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = f"{fn.__module__}.{fn.__qualname__}:{str(args)}:{str(sorted(kwargs.items()))}"
            val = cache.get(key)
            if val is not None:
                return val
            res = fn(*args, **kwargs)
            cache.set(key, res, ttl=ttl_seconds)
            return res
        return wrapper
    return decorator
