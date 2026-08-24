"""SessionSimulator — trading windows, daily cap, no-trade days, buffers."""

from __future__ import annotations

import random
from datetime import datetime, timezone, time as dt_time
from typing import Tuple, List, Optional, Set


def _parse_hm(hm: str) -> dt_time:
    h, m = map(int, hm.split(":"))
    return dt_time(hour=h, minute=m)


def _minutes_of_day(t: dt_time) -> int:
    return t.hour * 60 + t.minute


class SessionSimulator:
    """Simulates human trading sessions.

    All constants inside class.
    """

    LONDON_START = "07:30"
    LONDON_END = "16:00"
    NY_START = "08:00"
    NY_END = "17:30"

    # Default break 12:00-12:30
    DEFAULT_BREAKS: List[Tuple[str, str]] = [("12:00", "12:30")]

    DAILY_CAP_MIN = 3
    DAILY_CAP_MAX = 7

    NO_TRADE_DAY_PROB = 0.08

    SESSION_END_BUFFER_MIN_SEC = 10 * 60
    SESSION_END_BUFFER_MAX_SEC = 30 * 60

    def __init__(
        self,
        london_window: Tuple[str, str] = ("07:30", "16:00"),
        ny_window: Tuple[str, str] = ("08:00", "17:30"),
        breaks: Optional[List[Tuple[str, str]]] = None,
        daily_cap_range: Tuple[int, int] = (3, 7),
        seed: Optional[int] = None,
        config: Optional[object] = None,
    ):
        self._rng = random.Random(seed)

        # Config overrides
        if config is not None:
            london_window = config.london_window
            ny_window = config.ny_window
            breaks = config.session_breaks
            daily_cap_range = config.daily_cap_range
            self.NO_TRADE_DAY_PROB = config.no_trade_day_prob
            self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC = config.session_end_buffer_range
            self.DAILY_CAP_MIN, self.DAILY_CAP_MAX = daily_cap_range
            self.LONDON_START, self.LONDON_END = london_window
            self.NY_START, self.NY_END = ny_window
        else:
            self.LONDON_START, self.LONDON_END = london_window
            self.NY_START, self.NY_END = ny_window
            self.DAILY_CAP_MIN, self.DAILY_CAP_MAX = daily_cap_range

        self.breaks = breaks if breaks is not None else self.DEFAULT_BREAKS

        # Parsed windows
        self._london_start_min = _minutes_of_day(_parse_hm(self.LONDON_START))
        self._london_end_min = _minutes_of_day(_parse_hm(self.LONDON_END))
        self._ny_start_min = _minutes_of_day(_parse_hm(self.NY_START))
        self._ny_end_min = _minutes_of_day(_parse_hm(self.NY_END))

        self._breaks_min: List[Tuple[int, int]] = []
        for s, e in self.breaks:
            self._breaks_min.append((_minutes_of_day(_parse_hm(s)), _minutes_of_day(_parse_hm(e))))

        # Daily state
        self._current_day: Optional[datetime.date] = None
        self._daily_cap: int = self._rng.randint(self.DAILY_CAP_MIN, self.DAILY_CAP_MAX)
        self._orders_today: int = 0
        self._is_no_trade_day: bool = False
        self._session_end_buffer_sec: int = self._rng.randint(
            self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC
        )

    def _ensure_day(self, now_utc: datetime):
        day = now_utc.date()
        if self._current_day != day:
            self._current_day = day
            self._daily_cap = self._rng.randint(self.DAILY_CAP_MIN, self.DAILY_CAP_MAX)
            self._orders_today = 0
            self._is_no_trade_day = self._rng.random() < self.NO_TRADE_DAY_PROB
            self._session_end_buffer_sec = self._rng.randint(
                self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC
            )

    def is_weekend(self, now_utc: datetime) -> bool:
        # Monday=0 ... Sunday=6, weekend Sat/Sun
        return now_utc.weekday() >= 5

    def _minutes(self, now_utc: datetime) -> int:
        return now_utc.hour * 60 + now_utc.minute

    def _is_in_break(self, now_utc: datetime) -> bool:
        m = self._minutes(now_utc)
        for b_start, b_end in self._breaks_min:
            if b_start <= m < b_end:
                return True
        return False

    def is_in_trading_session(self, now_utc: datetime) -> bool:
        """Check if now is inside London OR NY window and not in break/weekend."""
        self._ensure_day(now_utc)
        if self.is_weekend(now_utc):
            return False
        if self._is_no_trade_day:
            return False
        if self._is_in_break(now_utc):
            return False
        m = self._minutes(now_utc)
        # London
        if self._london_start_min <= m < self._london_end_min:
            return True
        # NY
        if self._ny_start_min <= m < self._ny_end_min:
            return True
        return False

    def _session_end_minutes(self, now_utc: datetime) -> Optional[int]:
        """Return end minute of current session, or next ending session if outside? 
        For buffer check we need to know time to next session end."""
        m = self._minutes(now_utc)
        # If inside London, its end is relevant
        if self._london_start_min <= m < self._london_end_min:
            return self._london_end_min
        if self._ny_start_min <= m < self._ny_end_min:
            return self._ny_end_min
        # If before both, return earlier start's end? Simplify: check if before London end but after start etc.
        # For buffer: if outside session, we already block via is_in_trading_session, so buffer only matters inside.
        return None

    def is_in_session_end_buffer(self, now_utc: datetime) -> bool:
        """Buffer 10-30 min before session end — no new positions."""
        self._ensure_day(now_utc)
        if not self.is_in_trading_session(now_utc):
            # If not in session, buffer logic not needed (already blocked)
            return False
        end_min = self._session_end_minutes(now_utc)
        if end_min is None:
            return False
        m = self._minutes(now_utc)
        buffer_min = self._session_end_buffer_sec / 60.0
        return m >= (end_min - buffer_min)

    def can_open_new_order(self, now_utc: datetime) -> bool:
        """Full gate: session, weekend, no-trade day, daily cap, buffer."""
        self._ensure_day(now_utc)
        if self.is_weekend(now_utc):
            return False
        if self._is_no_trade_day:
            return False
        if self._orders_today >= self._daily_cap:
            return False
        if not self.is_in_trading_session(now_utc):
            return False
        if self.is_in_session_end_buffer(now_utc):
            return False
        return True

    def record_order(self, now_utc: datetime):
        self._ensure_day(now_utc)
        self._orders_today += 1

    def get_daily_cap(self) -> int:
        return self._daily_cap

    def get_orders_today(self) -> int:
        return self._orders_today

    def is_no_trade_day_today(self) -> bool:
        return self._is_no_trade_day

    def get_session_end_buffer_sec(self) -> int:
        return self._session_end_buffer_sec

    def reset(self):
        self._current_day = None
        self._orders_today = 0
        self._daily_cap = self._rng.randint(self.DAILY_CAP_MIN, self.DAILY_CAP_MAX)
        self._is_no_trade_day = False
        self._session_end_buffer_sec = self._rng.randint(
            self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC
        )

    # For testing: force new day
    def force_new_day(self, now_utc: datetime):
        self._current_day = None
        self._ensure_day(now_utc)
