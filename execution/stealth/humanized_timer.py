"""HumanizedTimer — randomized execution delays with news-awareness, fatigue, earnings."""

from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Optional, Set


class HumanizedTimer:
    """Randomized delays to mimic human reaction.

    All constants live inside the class (no hardcode outside stealth modules).
    Accepts optional seed for reproducibility.
    Supports news_calendar and earnings_calendar.
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
        earnings_calendar: Optional[List[Dict]] = None,
        seed: Optional[int] = None,
        config: Optional[object] = None,
    ):
        """
        Args:
            news_calendar: list of dicts {'time': datetime, 'impact': 'high'}
            earnings_calendar: list of dicts {'ticker': str, 'date': date}
            seed: optional seed for reproducibility
            config: optional StealthConfig to override constants
        """
        self._rng = random.Random(seed)
        self.news_calendar: List[Dict] = news_calendar or []
        self.earnings_calendar: List[Dict] = earnings_calendar or []

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

        # Pre-parse earnings dates for quick lookup
        self._earnings_by_ticker: Dict[str, Set[date]] = {}
        self._rebuild_earnings()

    def _rebuild_earnings(self):
        self._earnings_by_ticker.clear()
        for ev in self.earnings_calendar:
            ticker = str(ev.get("ticker", "")).upper()
            d = ev.get("date")
            if not ticker:
                continue
            # d can be date, datetime, or string
            parsed_date: Optional[date] = None
            if isinstance(d, date) and not isinstance(d, datetime):
                parsed_date = d
            elif isinstance(d, datetime):
                parsed_date = d.date()
            elif isinstance(d, str):
                try:
                    # Try ISO format
                    parsed_date = datetime.fromisoformat(d).date()
                except Exception:
                    continue
            if parsed_date:
                self._earnings_by_ticker.setdefault(ticker, set()).add(parsed_date)

    def update_earnings_calendar(self, earnings_calendar: List[Dict]):
        self.earnings_calendar = earnings_calendar or []
        self._rebuild_earnings()

    def update_news_calendar(self, news_calendar: List[Dict]):
        self.news_calendar = news_calendar or []

    def is_earnings_day(self, ticker: str, check_date: date) -> bool:
        """Check if ticker has earnings on check_date."""
        if not ticker:
            return False
        ticker = ticker.upper()
        dates = self._earnings_by_ticker.get(ticker)
        if not dates:
            return False
        return check_date in dates

    def _ensure_day(self, now_utc: datetime):
        """Reset daily state on new day."""
        day = now_utc.date() if isinstance(now_utc, datetime) else now_utc
        if self._current_day != day:
            self._current_day = day
            self._orders_today = 0
            self._fatigue_drift = 0.0
            self._current_min_gap_sec = self._rng.randint(self.MIN_GAP_MIN_SEC, self.MIN_GAP_MAX_SEC)

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

        fatigue = min(
            self._orders_today * self.FATIGUE_DRIFT_PER_ORDER,
            self.FATIGUE_DRIFT_MAX,
        )
        delay += fatigue

        if self._is_news_window(now_utc):
            news_extra = self._rng.uniform(self.NEWS_EXTRA_MIN, self.NEWS_EXTRA_MAX)
            delay += news_extra

        if self._rng.random() < self.HESITATION_PROB:
            hesitation = self._rng.uniform(self.HESITATION_MIN, self.HESITATION_MAX)
            delay += hesitation

        return round(delay, 3)

    def get_close_delay(self, now_utc: Optional[datetime] = None) -> float:
        """Random close delay."""
        if now_utc is not None:
            self._ensure_day(now_utc)
        base = self._rng.uniform(self.CLOSE_DELAY_MIN, self.CLOSE_DELAY_MAX)
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
        if self._last_order_time.date() != now_utc.date():
            return True
        elapsed = (now_utc - self._last_order_time).total_seconds()
        return elapsed >= self._current_min_gap_sec

    def get_current_min_gap(self) -> int:
        return self._current_min_gap_sec

    def record_order(self, now_utc: datetime):
        self._ensure_day(now_utc)
        self._last_order_time = now_utc
        self._orders_today += 1
        self._fatigue_drift = min(
            self._orders_today * self.FATIGUE_DRIFT_PER_ORDER,
            self.FATIGUE_DRIFT_MAX,
        )

    def reset(self):
        self._orders_today = 0
        self._current_day = None
        self._last_order_time = None
        self._fatigue_drift = 0.0
        self._current_min_gap_sec = self._rng.randint(self.MIN_GAP_MIN_SEC, self.MIN_GAP_MAX_SEC)

    def sample_delays(self, n: int, now_utc: datetime) -> List[float]:
        return [self.get_entry_delay(now_utc) for _ in range(n)]
