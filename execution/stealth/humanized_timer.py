"""HumanizedTimer — randomized execution delays with news-awareness and fatigue."""

from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional


class HumanizedTimer:
    """Randomized delays to mimic human reaction.

    All constants live inside the class (no hardcode outside stealth modules).
    Accepts optional seed for reproducibility.
    """

    # Base reaction 2.5-8s + jitter execution
    BASE_REACTION_MIN = 2.5
    BASE_REACTION_MAX = 8.0
    EXECUTION_JITTER_MIN = 0.1
    EXECUTION_JITTER_MAX = 1.5

    # News-aware: +15-90s if signal in window ±2min from high-impact news
    NEWS_EXTRA_MIN = 15.0
    NEWS_EXTRA_MAX = 90.0
    NEWS_WINDOW_SEC = 120  # ±2 min

    # Cumulative fatigue drift
    FATIGUE_DRIFT_PER_ORDER = 0.3
    FATIGUE_DRIFT_MAX = 5.0

    # Hesitation 8% chance +5-20s
    HESITATION_PROB = 0.08
    HESITATION_MIN = 5.0
    HESITATION_MAX = 20.0

    # Min gap between orders 3-15 min
    MIN_GAP_MIN_SEC = 3 * 60
    MIN_GAP_MAX_SEC = 15 * 60

    # Random close delay
    CLOSE_DELAY_MIN = 1.0
    CLOSE_DELAY_MAX = 5.0

    def __init__(
        self,
        news_calendar: Optional[List[Dict]] = None,
        seed: Optional[int] = None,
        config: Optional[object] = None,
    ):
        """
        Args:
            news_calendar: list of dicts {'time': datetime, 'impact': 'high'}
            seed: optional seed for reproducibility
            config: optional StealthConfig to override constants
        """
        self._rng = random.Random(seed)
        self.news_calendar: List[Dict] = news_calendar or []

        # Apply config overrides if provided
        if config is not None:
            self.BASE_REACTION_MIN, self.BASE_REACTION_MAX = config.timer_base_reaction_range
            self.EXECUTION_JITTER_MIN, self.EXECUTION_JITTER_MAX = config.timer_execution_jitter_range
            self.NEWS_EXTRA_MIN, self.NEWS_EXTRA_MAX = config.timer_news_extra_range
            self.NEWS_WINDOW_SEC = config.timer_news_window_sec
            self.FATIGUE_DRIFT_PER_ORDER = config.timer_fatigue_drift_per_order
            self.FATIGUE_DRIFT_MAX = config.timer_fatigue_drift_max
            self.HESITATION_PROB = config.timer_hesitation_prob
            self.HESITATION_MIN, self.HESITATION_MAX = config.timer_hesitation_range
            self.MIN_GAP_MIN_SEC, self.MIN_GAP_MAX_SEC = config.timer_min_gap_range
            self.CLOSE_DELAY_MIN, self.CLOSE_DELAY_MAX = config.timer_close_delay_range

        # Stateful tracking
        self._orders_today: int = 0
        self._current_day: Optional[datetime.date] = None
        self._current_min_gap_sec: int = self._rng.randint(self.MIN_GAP_MIN_SEC, self.MIN_GAP_MAX_SEC)
        self._last_order_time: Optional[datetime] = None
        self._fatigue_drift: float = 0.0

    def _ensure_day(self, now_utc: datetime):
        """Reset daily state on new day."""
        day = now_utc.date() if isinstance(now_utc, datetime) else now_utc
        if self._current_day != day:
            self._current_day = day
            self._orders_today = 0
            self._fatigue_drift = 0.0
            self._current_min_gap_sec = self._rng.randint(self.MIN_GAP_MIN_SEC, self.MIN_GAP_MAX_SEC)
            # Note: _last_order_time is NOT reset on new day? Keep it for gap across midnight
            # but for simplicity reset gap check on new day by allowing first order.
            # We'll keep last_order_time but is_min_gap_ok will allow if new day.

    def _is_news_window(self, now_utc: datetime) -> bool:
        """Check if now is within ±NEWS_WINDOW_SEC of high-impact news."""
        if not self.news_calendar:
            return False
        for ev in self.news_calendar:
            ev_time = ev.get("time")
            impact = ev.get("impact", "").lower()
            if impact != "high":
                continue
            if not isinstance(ev_time, datetime):
                continue
            # Ensure timezone aware
            if ev_time.tzinfo is None:
                ev_time = ev_time.replace(tzinfo=timezone.utc)
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            delta = abs((now_utc - ev_time).total_seconds())
            if delta <= self.NEWS_WINDOW_SEC:
                return True
        return False

    def get_entry_delay(self, now_utc: datetime) -> float:
        """Calculate humanized entry delay in seconds."""
        self._ensure_day(now_utc)

        base = self._rng.uniform(self.BASE_REACTION_MIN, self.BASE_REACTION_MAX)
        jitter = self._rng.uniform(self.EXECUTION_JITTER_MIN, self.EXECUTION_JITTER_MAX)
        delay = base + jitter

        # Fatigue drift accumulates with orders today
        fatigue = min(
            self._orders_today * self.FATIGUE_DRIFT_PER_ORDER,
            self.FATIGUE_DRIFT_MAX,
        )
        delay += fatigue

        # News-aware extra
        if self._is_news_window(now_utc):
            news_extra = self._rng.uniform(self.NEWS_EXTRA_MIN, self.NEWS_EXTRA_MAX)
            delay += news_extra

        # Hesitation 8% chance
        if self._rng.random() < self.HESITATION_PROB:
            hesitation = self._rng.uniform(self.HESITATION_MIN, self.HESITATION_MAX)
            delay += hesitation

        return round(delay, 3)

    def get_close_delay(self, now_utc: Optional[datetime] = None) -> float:
        """Random close delay."""
        if now_utc is not None:
            self._ensure_day(now_utc)
        base = self._rng.uniform(self.CLOSE_DELAY_MIN, self.CLOSE_DELAY_MAX)
        # Add small fatigue component
        fatigue = min(
            self._orders_today * 0.1,
            2.0,
        )
        return round(base + fatigue, 3)

    def is_min_gap_ok(self, now_utc: datetime) -> bool:
        """Check if minimal gap between orders is satisfied."""
        self._ensure_day(now_utc)
        if self._last_order_time is None:
            return True
        # If new day, allow (reset)
        if self._last_order_time.date() != now_utc.date():
            return True
        elapsed = (now_utc - self._last_order_time).total_seconds()
        return elapsed >= self._current_min_gap_sec

    def get_current_min_gap(self) -> int:
        """Return current min gap in seconds (for logging/testing)."""
        return self._current_min_gap_sec

    def record_order(self, now_utc: datetime):
        """Record that an order was placed at now_utc."""
        self._ensure_day(now_utc)
        self._last_order_time = now_utc
        self._orders_today += 1
        self._fatigue_drift = min(
            self._orders_today * self.FATIGUE_DRIFT_PER_ORDER,
            self.FATIGUE_DRIFT_MAX,
        )

    def reset(self):
        """Reset all state (for tests)."""
        self._orders_today = 0
        self._current_day = None
        self._last_order_time = None
        self._fatigue_drift = 0.0
        self._current_min_gap_sec = self._rng.randint(self.MIN_GAP_MIN_SEC, self.MIN_GAP_MAX_SEC)

    # For testing distribution
    def sample_delays(self, n: int, now_utc: datetime) -> List[float]:
        return [self.get_entry_delay(now_utc) for _ in range(n)]
