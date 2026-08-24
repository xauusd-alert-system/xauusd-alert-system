"""OrderHygiene — magic numbers, comments, API jitter."""

from __future__ import annotations

import random
from typing import List, Tuple, Optional


class OrderHygiene:
    """Pool of 20 magic numbers outside EA ranges, rotation, comments, jitter."""

    MAGIC_POOL_SIZE = 20
    BANNED_RANGES: List[Tuple[int, int]] = [(0, 100), (70000000, 89000000)]
    ALLOWED_RANGES: List[Tuple[int, int]] = [(101, 69999999), (89000001, 99999999)]

    COMMENT_EMPTY_PROB = 0.70
    COMMENT_POOL: List[str] = ["xau", "gold long", "scalp", "news play"]

    API_JITTER_MIN_MS = 50
    API_JITTER_MAX_MS = 350

    def __init__(
        self,
        seed: Optional[int] = None,
        config: Optional[object] = None,
        magic_pool: Optional[List[int]] = None,
    ):
        self._rng = random.Random(seed)

        if config is not None:
            self.MAGIC_POOL_SIZE = config.magic_pool_size
            self.BANNED_RANGES = config.magic_banned_ranges
            self.ALLOWED_RANGES = config.magic_allowed_ranges
            self.COMMENT_EMPTY_PROB = config.comment_empty_prob
            self.COMMENT_POOL = config.comment_pool
            self.API_JITTER_MIN_MS, self.API_JITTER_MAX_MS = config.api_jitter_range_ms

        if magic_pool is not None:
            self._magic_pool = magic_pool
        else:
            self._magic_pool = self._generate_magic_pool()

        self._last_magic: Optional[int] = None
        self._next_idx: int = self._rng.randint(0, len(self._magic_pool) - 1) if self._magic_pool else 0

    def _is_banned(self, n: int) -> bool:
        for low, high in self.BANNED_RANGES:
            if low <= n <= high:
                return True
        return False

    def _generate_magic_pool(self) -> List[int]:
        pool: List[int] = []
        attempts = 0
        max_attempts = self.MAGIC_POOL_SIZE * 100
        while len(pool) < self.MAGIC_POOL_SIZE and attempts < max_attempts:
            attempts += 1
            # Pick allowed range randomly
            r = self._rng.choice(self.ALLOWED_RANGES)
            low, high = r
            candidate = self._rng.randint(low, high)
            if candidate in pool:
                continue
            if self._is_banned(candidate):
                continue
            pool.append(candidate)
        # Fallback deterministic if random failed
        if len(pool) < self.MAGIC_POOL_SIZE:
            # Generate sequential outside banned
            candidate = 101
            while len(pool) < self.MAGIC_POOL_SIZE:
                if not self._is_banned(candidate) and candidate not in pool:
                    pool.append(candidate)
                candidate += 997  # prime step to spread
                if candidate > 99999999:
                    candidate = 101 + len(pool) * 1000
        self._rng.shuffle(pool)
        return pool

    def get_magic_pool(self) -> List[int]:
        return list(self._magic_pool)

    def get_next_magic(self) -> int:
        """Rotation without repeat consecutively."""
        if not self._magic_pool:
            raise ValueError("Magic pool empty")
        # If only one, return it
        if len(self._magic_pool) == 1:
            self._last_magic = self._magic_pool[0]
            return self._last_magic

        # Try up to 10 times to avoid repeat
        for _ in range(10):
            candidate = self._magic_pool[self._next_idx]
            self._next_idx = (self._next_idx + 1) % len(self._magic_pool)
            if candidate != self._last_magic:
                self._last_magic = candidate
                return candidate

        # Fallback: pick random different
        choices = [m for m in self._magic_pool if m != self._last_magic]
        chosen = self._rng.choice(choices) if choices else self._magic_pool[0]
        self._last_magic = chosen
        return chosen

    def get_comment(self) -> str:
        """70% empty string, else human shorthand."""
        if self._rng.random() < self.COMMENT_EMPTY_PROB:
            return ""
        return self._rng.choice(self.COMMENT_POOL)

    def get_api_jitter_ms(self) -> int:
        """50-350ms jitter for OrderSend."""
        return self._rng.randint(self.API_JITTER_MIN_MS, self.API_JITTER_MAX_MS)

    def get_api_jitter_sec(self) -> float:
        return self.get_api_jitter_ms() / 1000.0

    def validate_magic(self, magic: int) -> bool:
        """Check magic not in banned EA ranges."""
        return not self._is_banned(magic)

    def reset(self):
        self._last_magic = None
        self._next_idx = self._rng.randint(0, len(self._magic_pool) - 1) if self._magic_pool else 0
