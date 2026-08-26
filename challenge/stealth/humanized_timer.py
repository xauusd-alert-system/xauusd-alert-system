"""HumanizedTimer — randomized delays before browser actions.

Provides human-like timing that defeats session-recording and
fingerprint-based bot detection (Hash Hedge Clause 6.5c).

All constants live inside the class.  Every public method accepts an
optional *rng* parameter (or falls back to the instance RNG seeded at
__init__) so unit tests can pin randomness.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("stealth.timer")

# ---------------------------------------------------------------------------
# News / earnings calendar stubs (wire to news.calendar_feed or empty)
# ---------------------------------------------------------------------------

class _EmptyNewsFeed:
    """Stub when no real feed is wired."""
    def get_high_impact(self, **_kw):  # noqa: D401
        return []

class _EmptyEarningsCalendar:
    """Stub — empty dict means no earnings days known."""
    def is_earnings_day(self, ticker: str, d) -> bool:  # noqa: ARG002
        return False


class HumanizedTimer:
    """Randomised delays that mimic organic browser interaction.

    Parameters
    ----------
    seed : int | None
        Optional RNG seed for reproducible tests.
    news_calendar : list[dict] | None
        ``[{'time': datetime, 'impact': 'high'}, ...]``  — merged into the
        delay decision so the bot pauses longer before signals near high-impact
        events.
    earnings_calendar : object | None
        Must expose ``is_earnings_day(ticker, date) -> bool``.  Falls back to
        a no-op stub when *None*.
    """

    # All tuning constants live here — no external hard-coding.
    BASE_DELAY_MIN_S: float = 2.5
    BASE_DELAY_MAX_S: float = 8.0
    JITTER_MIN_S: float = 0.1
    JITTER_MAX_S: float = 1.5
    FATIGUE_DRIFT_PER_ORDER_S: float = 0.3
    FATIGUE_MAX_S: float = 5.0
    HESITATION_CHANCE: float = 0.08          # 8 %
    HESITATION_MIN_S: float = 5.0
    HESITATION_MAX_S: float = 20.0
    MIN_GAP_MINUTES: float = 3.0
    MAX_GAP_MINUTES: float = 15.0
    CLOSE_DELAY_MIN_S: float = 1.0
    CLOSE_DELAY_MAX_S: float = 5.0
    NEWS_BUFFER_MIN_S: float = 15.0          # +15 s
    NEWS_BUFFER_MAX_S: float = 90.0          # +90 s
    NEWS_WINDOW_MINUTES: float = 2.0         # ±2 min

    def __init__(
        self,
        *,
        seed: int | None = None,
        news_calendar: Optional[List[Dict[str, Any]]] = None,
        earnings_calendar: Any = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._orders_today: int = 0
        self._last_action_ts: Optional[datetime] = None

        # --- news feed wiring ---
        self._news_events: List[Dict[str, Any]] = list(news_calendar or [])
        # Accept both raw dicts and CalendarEvent objects
        self._calendar_feed = getattr(news_calendar, "_feed", None) or _EmptyNewsFeed()

        # --- earnings calendar wiring ---
        self._earnings = earnings_calendar or _EmptyEarningsCalendar()

    # ------------------------------------------------------------------
    # Order counter (fatigue drift)
    # ------------------------------------------------------------------

    def reset_orders_today(self) -> None:
        self._orders_today = 0

    def _record_order(self) -> None:
        self._orders_today += 1

    # ------------------------------------------------------------------
    # Core delay methods
    # ------------------------------------------------------------------

    def base_delay(self) -> float:
        """Random base human reaction time in seconds."""
        return self._rng.uniform(self.BASE_DELAY_MIN_S, self.BASE_DELAY_MAX_S)

    def jitter(self) -> float:
        """Small execution jitter added on top of base delay."""
        return self._rng.uniform(self.JITTER_MIN_S, self.JITTER_MAX_S)

    def fatigue_drift(self) -> float:
        """Cumulative fatigue from orders executed today."""
        return min(
            self._orders_today * self.FATIGUE_DRIFT_PER_ORDER_S,
            self.FATIGUE_MAX_S,
        )

    def hesitation_delay(self) -> float:
        """8 % chance of a long human-like pause (5-20 s)."""
        if self._rng.random() < self.HESITATION_CHANCE:
            return self._rng.uniform(self.HESITATION_MIN_S, self.HESITATION_MAX_S)
        return 0.0

    def news_aware_delay(self, signal_time: datetime) -> float:
        """Extra delay when the signal falls near a high-impact news event.

        Returns a value in [NEWS_BUFFER_MIN_S .. NEWS_BUFFER_MAX_S] if the
        *signal_time* is within ±NEWS_WINDOW_MINUTES of a high-impact event,
        otherwise 0.
        """
        for ev in self._news_events:
            ev_time = ev.get("time")
            if ev_time is None:
                continue
            if ev.get("impact", "").lower() != "high":
                continue
            diff_min = abs((ev_time - signal_time).total_seconds()) / 60.0
            if diff_min <= self.NEWS_WINDOW_MINUTES:
                delay = self._rng.uniform(self.NEWS_BUFFER_MIN_S, self.NEWS_BUFFER_MAX_S)
                logger.debug(
                    "news-aware delay %.1fs (event '%s' %.0f min away)",
                    delay, ev.get("title", "?"), diff_min,
                )
                return delay
        return 0.0

    def close_delay(self) -> float:
        """Random delay before closing a position (1-5 s)."""
        return self._rng.uniform(self.CLOSE_DELAY_MIN_S, self.CLOSE_DELAY_MAX_S)

    def compute_delay(self, now: datetime) -> float:
        """Full composite delay for a pre-action pause.

        Combines base + jitter + fatigue + hesitation + news-aware.
        Does NOT call ``_record_order`` — caller must do that after
        the action completes.
        """
        delay = self.base_delay() + self.jitter() + self.fatigue_drift()
        delay += self.hesitation_delay()
        delay += self.news_aware_delay(now)
        return delay

    def record_action(self) -> None:
        """Record that an action was performed (advances fatigue counter).

        Call *after* the action, not before.
        """
        self._record_order()

    # ------------------------------------------------------------------
    # Gap check
    # ------------------------------------------------------------------

    def is_min_gap_ok(self, last_action_ts: Optional[datetime], now: datetime) -> bool:
        """Return True if enough time has elapsed since *last_action_ts*.

        Enforces a minimum gap of MIN_GAP_MINUTES between browser actions
        (e.g. switching tabs, placing orders).
        """
        if last_action_ts is None:
            return True
        gap_min = (now - last_action_ts).total_seconds() / 60.0
        ok = gap_min >= self.MIN_GAP_MINUTES
        if not ok:
            logger.debug(
                "min gap not met: %.1f min < %.1f min", gap_min, self.MIN_GAP_MINUTES,
            )
        return ok

    def max_gap_minutes(self) -> float:
        """Upper bound for the min-gap window (for test introspection)."""
        return self.MAX_GAP_MINUTES

    # ------------------------------------------------------------------
    # Earnings helpers
    # ------------------------------------------------------------------

    def is_earnings_day(self, ticker: str, d: date | None = None) -> bool:
        """Delegate to the configured earnings calendar."""
        d = d or date.today()
        return self._earnings.is_earnings_day(ticker, d)

    def set_news_events(self, events: List[Dict[str, Any]]) -> None:
        """Hot-swap the news event list at runtime."""
        self._news_events = list(events)
