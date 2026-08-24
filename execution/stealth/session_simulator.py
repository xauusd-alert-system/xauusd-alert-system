"""SessionSimulator — trading windows for both MT5 and UTEx challenge (ET)."""

from __future__ import annotations

import random
from datetime import datetime, timezone, time as dt_time, date, timedelta
from typing import Tuple, List, Optional, Set


def _parse_hm(hm: str) -> dt_time:
    h, m = map(int, hm.split(":"))
    return dt_time(hour=h, minute=m)


def _minutes_of_day(t: dt_time) -> int:
    return t.hour * 60 + t.minute


def _minutes_from_dt(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


class SessionSimulator:
    """Simulates human trading sessions for MT5 and UTEx challenge.

    All constants inside class. Supports ET windows, tab lifecycle, holidays, min 5 days.
    """

    LONDON_START = "07:30"
    LONDON_END = "16:00"
    NY_START = "08:00"
    NY_END = "17:30"

    # ET windows for UTEx ORB challenge
    ET_RANGE_START = "09:30"
    ET_RANGE_END = "09:45"
    ET_ENTRY_START = "09:45"
    ET_ENTRY_END = "10:30"
    ET_CLOSE_ALL = "15:30"
    ET_TAB_OPEN_START = "09:20"
    ET_TAB_OPEN_END = "09:28"
    ET_WIND_DOWN_START = "10:30"
    ET_WIND_DOWN_END = "11:00"

    DEFAULT_BREAKS: List[Tuple[str, str]] = [("12:00", "12:30")]

    DAILY_CAP_MIN = 3
    DAILY_CAP_MAX = 7
    CHALLENGE_DAILY_CAP = 2

    NO_TRADE_DAY_PROB = 0.08

    SESSION_END_BUFFER_MIN_SEC = 10 * 60
    SESSION_END_BUFFER_MAX_SEC = 30 * 60

    MARKET_HOLIDAYS: List[str] = [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    ]

    MIN_TRADING_DAYS = 5

    def __init__(
        self,
        london_window: Tuple[str, str] = ("07:30", "16:00"),
        ny_window: Tuple[str, str] = ("08:00", "17:30"),
        et_range_window: Tuple[str, str] = ("09:30", "09:45"),
        et_entry_window: Tuple[str, str] = ("09:45", "10:30"),
        breaks: Optional[List[Tuple[str, str]]] = None,
        daily_cap_range: Tuple[int, int] = (3, 7),
        challenge_daily_cap: int = 2,
        seed: Optional[int] = None,
        config: Optional[object] = None,
        use_et: bool = False,
    ):
        self._rng = random.Random(seed)
        self.use_et = use_et

        if config is not None:
            london_window = config.london_window
            ny_window = config.ny_window
            et_range_window = config.et_range_window
            et_entry_window = config.et_entry_window
            self.ET_CLOSE_ALL = config.et_close_all_time
            self.ET_TAB_OPEN_START, self.ET_TAB_OPEN_END = config.et_tab_open_window
            self.ET_WIND_DOWN_START, self.ET_WIND_DOWN_END = config.et_wind_down_window
            breaks = config.session_breaks
            daily_cap_range = config.daily_cap_range
            challenge_daily_cap = config.challenge_daily_cap
            self.NO_TRADE_DAY_PROB = config.no_trade_day_prob
            self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC = config.session_end_buffer_range
            self.DAILY_CAP_MIN, self.DAILY_CAP_MAX = daily_cap_range
            self.CHALLENGE_DAILY_CAP = challenge_daily_cap
            self.LONDON_START, self.LONDON_END = london_window
            self.NY_START, self.NY_END = ny_window
            self.ET_RANGE_START, self.ET_RANGE_END = et_range_window
            self.ET_ENTRY_START, self.ET_ENTRY_END = et_entry_window
            self.MARKET_HOLIDAYS = config.market_holidays
            self.MIN_TRADING_DAYS = config.challenge_min_trading_days
        else:
            self.LONDON_START, self.LONDON_END = london_window
            self.NY_START, self.NY_END = ny_window
            self.ET_RANGE_START, self.ET_RANGE_END = et_range_window
            self.ET_ENTRY_START, self.ET_ENTRY_END = et_entry_window
            self.DAILY_CAP_MIN, self.DAILY_CAP_MAX = daily_cap_range
            self.CHALLENGE_DAILY_CAP = challenge_daily_cap

        self.breaks = breaks if breaks is not None else self.DEFAULT_BREAKS

        # Parsed windows MT5
        self._london_start_min = _minutes_of_day(_parse_hm(self.LONDON_START))
        self._london_end_min = _minutes_of_day(_parse_hm(self.LONDON_END))
        self._ny_start_min = _minutes_of_day(_parse_hm(self.NY_START))
        self._ny_end_min = _minutes_of_day(_parse_hm(self.NY_END))

        # ET windows
        self._et_range_start_min = _minutes_of_day(_parse_hm(self.ET_RANGE_START))
        self._et_range_end_min = _minutes_of_day(_parse_hm(self.ET_RANGE_END))
        self._et_entry_start_min = _minutes_of_day(_parse_hm(self.ET_ENTRY_START))
        self._et_entry_end_min = _minutes_of_day(_parse_hm(self.ET_ENTRY_END))
        self._et_close_all_min = _minutes_of_day(_parse_hm(self.ET_CLOSE_ALL))
        self._et_tab_open_start_min = _minutes_of_day(_parse_hm(self.ET_TAB_OPEN_START))
        self._et_tab_open_end_min = _minutes_of_day(_parse_hm(self.ET_TAB_OPEN_END))
        self._et_wind_down_start_min = _minutes_of_day(_parse_hm(self.ET_WIND_DOWN_START))
        self._et_wind_down_end_min = _minutes_of_day(_parse_hm(self.ET_WIND_DOWN_END))

        self._breaks_min: List[Tuple[int, int]] = []
        for s, e in self.breaks:
            self._breaks_min.append((_minutes_of_day(_parse_hm(s)), _minutes_of_day(_parse_hm(e))))

        self._holidays_set: Set[date] = set()
        for h in self.MARKET_HOLIDAYS:
            try:
                self._holidays_set.add(datetime.fromisoformat(h).date())
            except Exception:
                continue

        # Daily state
        self._current_day: Optional[date] = None
        self._daily_cap: int = self._rng.randint(self.DAILY_CAP_MIN, self.DAILY_CAP_MAX) if not use_et else self.CHALLENGE_DAILY_CAP
        self._orders_today: int = 0
        self._is_no_trade_day: bool = False
        self._session_end_buffer_sec: int = self._rng.randint(
            self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC
        )
        # Tab lifecycle
        self._tab_open_time_min: int = self._rng.randint(self._et_tab_open_start_min, self._et_tab_open_end_min)
        self._wind_down_time_min: int = self._rng.randint(self._et_wind_down_start_min, self._et_wind_down_end_min)

        # Min 5 trading days tracking
        self._trading_days_count: int = 0
        self._trading_days_set: Set[date] = set()

    def _ensure_day(self, now_utc: datetime):
        day = now_utc.date()
        if self._current_day != day:
            self._current_day = day
            if self.use_et:
                self._daily_cap = self.CHALLENGE_DAILY_CAP
            else:
                self._daily_cap = self._rng.randint(self.DAILY_CAP_MIN, self.DAILY_CAP_MAX)
            self._orders_today = 0
            self._is_no_trade_day = self._rng.random() < self.NO_TRADE_DAY_PROB
            self._session_end_buffer_sec = self._rng.randint(
                self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC
            )
            self._tab_open_time_min = self._rng.randint(self._et_tab_open_start_min, self._et_tab_open_end_min)
            self._wind_down_time_min = self._rng.randint(self._et_wind_down_start_min, self._et_wind_down_end_min)

    def is_weekend(self, now_utc: datetime) -> bool:
        return now_utc.weekday() >= 5

    def is_holiday(self, now_utc: datetime) -> bool:
        return now_utc.date() in self._holidays_set

    def _minutes(self, now_utc: datetime) -> int:
        return now_utc.hour * 60 + now_utc.minute

    def _is_in_break(self, now_utc: datetime) -> bool:
        m = self._minutes(now_utc)
        for b_start, b_end in self._breaks_min:
            if b_start <= m < b_end:
                return True
        return False

    def is_in_trading_session(self, now_utc: datetime) -> bool:
        """Check session. If use_et True, use ET windows, else London/NY."""
        self._ensure_day(now_utc)
        if self.is_weekend(now_utc):
            return False
        if self.is_holiday(now_utc):
            return False
        if self._is_no_trade_day:
            return False
        if self.use_et:
            # For challenge, trading session is 9:30-10:30 ET (range+entry)
            # But we consider range window + entry window as trading session
            m = self._minutes(now_utc)
            # Range window 9:30-9:45 and entry 9:45-10:30
            if self._et_range_start_min <= m < self._et_entry_end_min:
                if not self._is_in_break(now_utc):
                    return True
            return False
        else:
            if self._is_in_break(now_utc):
                return False
            m = self._minutes(now_utc)
            if self._london_start_min <= m < self._london_end_min:
                return True
            if self._ny_start_min <= m < self._ny_end_min:
                return True
            return False

    def is_in_entry_window(self, now_utc: datetime) -> bool:
        """For ET challenge: 9:45-10:30 only."""
        self._ensure_day(now_utc)
        if not self.use_et:
            return self.is_in_trading_session(now_utc)
        m = self._minutes(now_utc)
        return self._et_entry_start_min <= m < self._et_entry_end_min

    def is_in_range_window(self, now_utc: datetime) -> bool:
        self._ensure_day(now_utc)
        m = self._minutes(now_utc)
        return self._et_range_start_min <= m < self._et_range_end_min

    def should_close_all(self, now_utc: datetime) -> bool:
        """Check if time to close all positions (15:30 ET)."""
        m = self._minutes(now_utc)
        return m >= self._et_close_all_min

    def _session_end_minutes(self, now_utc: datetime) -> Optional[int]:
        m = self._minutes(now_utc)
        if self.use_et:
            if self._et_range_start_min <= m < self._et_entry_end_min:
                return self._et_entry_end_min
            return None
        else:
            if self._london_start_min <= m < self._london_end_min:
                return self._london_end_min
            if self._ny_start_min <= m < self._ny_end_min:
                return self._ny_end_min
            return None

    def is_in_session_end_buffer(self, now_utc: datetime) -> bool:
        self._ensure_day(now_utc)
        if not self.is_in_trading_session(now_utc):
            return False
        end_min = self._session_end_minutes(now_utc)
        if end_min is None:
            return False
        m = self._minutes(now_utc)
        buffer_min = self._session_end_buffer_sec / 60.0
        return m >= (end_min - buffer_min)

    def can_open_new_order(self, now_utc: datetime) -> bool:
        self._ensure_day(now_utc)
        if self.is_weekend(now_utc):
            return False
        if self.is_holiday(now_utc):
            return False
        if self._is_no_trade_day:
            return False
        if self._orders_today >= self._daily_cap:
            return False
        if self.use_et:
            if not self.is_in_entry_window(now_utc):
                return False
        else:
            if not self.is_in_trading_session(now_utc):
                return False
        if self.is_in_session_end_buffer(now_utc):
            return False
        return True

    def get_tab_open_time(self) -> Tuple[int, int]:
        """Return (hour, minute) for tab open 9:20-9:28 ET."""
        h = self._tab_open_time_min // 60
        m = self._tab_open_time_min % 60
        return h, m

    def get_wind_down_time(self) -> Tuple[int, int]:
        h = self._wind_down_time_min // 60
        m = self._wind_down_time_min % 60
        return h, m

    def is_in_wind_down(self, now_utc: datetime) -> bool:
        m = self._minutes(now_utc)
        return self._et_wind_down_start_min <= m < self._et_wind_down_end_min

    def record_order(self, now_utc: datetime):
        self._ensure_day(now_utc)
        self._orders_today += 1
        # Track trading days for min 5 requirement
        if self._current_day not in self._trading_days_set:
            self._trading_days_set.add(self._current_day)
            self._trading_days_count += 1

    def get_trading_days_count(self) -> int:
        return self._trading_days_count

    def needs_more_trading_days(self) -> bool:
        return self._trading_days_count < self.MIN_TRADING_DAYS

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
        self._daily_cap = self._rng.randint(self.DAILY_CAP_MIN, self.DAILY_CAP_MAX) if not self.use_et else self.CHALLENGE_DAILY_CAP
        self._is_no_trade_day = False
        self._session_end_buffer_sec = self._rng.randint(
            self.SESSION_END_BUFFER_MIN_SEC, self.SESSION_END_BUFFER_MAX_SEC
        )
        self._tab_open_time_min = self._rng.randint(self._et_tab_open_start_min, self._et_tab_open_end_min)
        self._wind_down_time_min = self._rng.randint(self._et_wind_down_start_min, self._et_wind_down_end_min)

    def force_new_day(self, now_utc: datetime):
        self._current_day = None
        self._ensure_day(now_utc)
