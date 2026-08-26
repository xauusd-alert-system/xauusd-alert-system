"""SessionSimulator — manages the ORB session lifecycle (9:30-10:30 ET).

Controls tab open/close timing, daily caps, weekend/holiday filtering,
wind-down period, and the 5-trading-day minimum.  All constants inside
the class; optional seed for deterministic tests.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, date, time, timedelta
from typing import Optional, List

logger = logging.getLogger("stealth.session")


class SessionSimulator:
    """Simulates a realistic trader's session around ORB hours.

    Parameters
    ----------
    seed : int | None
        Optional RNG seed.
    use_et : bool
        If True, uses US Eastern hours.  False → MT5 London/NY mode.
    cfg : dict | None
        Override dict with session windows, holidays, daily cap, etc.
    """

    # --- Defaults (all inside class) ---
    # ORB windows (ET)
    ORB_RANGE_START: str = "09:30"
    ORB_RANGE_END: str = "09:45"
    ORB_ENTRY_START: str = "09:45"
    ORB_ENTRY_END: str = "10:30"
    ALL_POSITIONS_CLOSE: str = "15:30"
    TAB_OPEN_MIN: str = "09:20"
    TAB_OPEN_MAX: str = "09:28"
    WIND_DOWN_MIN: str = "10:30"
    WIND_DOWN_MAX: str = "11:00"
    SKIP_DAY_CHANCE: float = 0.08        # 8 % chance of not trading today
    DAILY_TRADE_CAP: int = 2
    MIN_TRADING_DAYS: int = 5

    # MT5 mode windows
    MT5_LONDON_START: str = "07:30"
    MT5_LONDON_END: str = "16:00"
    MT5_NY_START: str = "08:00"
    MT5_NY_END: str = "17:30"

    # Market holidays (2026 US — extend as needed)
    _US_MARKET_HOLIDAYS_2026 = [
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # MLK Jr. Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day (observed)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26), # Thanksgiving
        date(2026, 12, 25), # Christmas
    ]

    def __init__(
        self,
        *,
        seed: int | None = None,
        use_et: bool = True,
        cfg: Optional[dict] = None,
    ) -> None:
        c = cfg or {}
        self._rng = random.Random(seed)
        self.use_et = c.get("use_et", use_et)

        # Override windows if provided
        self.orb_range_start = c.get("orb_range_start", self.ORB_RANGE_START)
        self.orb_range_end = c.get("orb_range_end", self.ORB_RANGE_END)
        self.orb_entry_start = c.get("orb_entry_start", self.ORB_ENTRY_START)
        self.orb_entry_end = c.get("orb_entry_end", self.ORB_ENTRY_END)
        self.all_close = c.get("all_positions_close", self.ALL_POSITIONS_CLOSE)
        self.tab_open_min = c.get("tab_open_min", self.TAB_OPEN_MIN)
        self.tab_open_max = c.get("tab_open_max", self.TAB_OPEN_MAX)
        self.wind_down_min = c.get("wind_down_min", self.WIND_DOWN_MIN)
        self.wind_down_max = c.get("wind_down_max", self.WIND_DOWN_MAX)
        self.skip_day_chance = c.get("skip_day_chance", self.SKIP_DAY_CHANCE)
        self.daily_trade_cap = c.get("daily_trade_cap", self.DAILY_TRADE_CAP)
        self.min_trading_days = c.get("min_trading_days", self.MIN_TRADING_DAYS)
        self.holidays: List[date] = c.get("holidays", self._US_MARKET_HOLIDAYS_2026)

        # State
        self._trades_today: int = 0
        self._trading_days_count: int = 0
        self._today: Optional[date] = None
        self._skip_today: bool = False
        self._positions_closed: bool = False

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def et_offset_hours(d: date) -> int:
        """US Eastern offset for a given date: -4 (EDT) in DST, -5 (EST) otherwise.

        DST runs from the 2nd Sunday of March (02:00 local) to the 1st Sunday
        of November (02:00 local).  Callers building ET datetimes for session
        checks should use this instead of a fixed -4 constant.
        """
        # Second Sunday of March
        first_mar = date(d.year, 3, 1)
        first_sun_mar = first_mar + timedelta(days=(6 - first_mar.weekday()) % 7)
        dst_start = first_sun_mar + timedelta(days=7)
        # First Sunday of November
        first_nov = date(d.year, 11, 1)
        dst_end = first_nov + timedelta(days=(6 - first_nov.weekday()) % 7)
        return -4 if dst_start <= d < dst_end else -5

    @staticmethod
    def _parse_hm(hm: str) -> time:
        h, m = map(int, hm.split(":"))
        return time(h, m)

    @staticmethod
    def _time_to_minutes(t: time) -> int:
        return t.hour * 60 + t.minute

    # ------------------------------------------------------------------
    # Weekend / holiday checks
    # ------------------------------------------------------------------

    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5  # Saturday=5, Sunday=6

    def is_market_holiday(self, d: date) -> bool:
        return d in self.holidays

    def is_tradeable_day(self, d: date) -> bool:
        return not self.is_weekend(d) and not self.is_market_holiday(d)

    # ------------------------------------------------------------------
    # Session phase checks (ET times)
    # ------------------------------------------------------------------

    def _minutes_of(self, hm: str) -> int:
        t = self._parse_hm(hm)
        return self._time_to_minutes(t)

    def get_tab_open_time(self, now_et: datetime) -> Optional[datetime]:
        """Random tab-open time between TAB_OPEN_MIN and TAB_OPEN_MAX ET.

        Returns None if we shouldn't open a tab (weekend, holiday, skip).
        """
        if not self._is_active_day(now_et.date()):
            return None
        lo = self._minutes_of(self.tab_open_min)
        hi = self._minutes_of(self.tab_open_max)
        minute = self._rng.randint(lo, hi)
        hour = minute // 60
        minute = minute % 60
        return now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def is_in_range_phase(self, now_et: datetime) -> bool:
        """True during 9:30-9:45 ET (ORB range accumulation)."""
        t = self._time_to_minutes(now_et.time())
        lo = self._minutes_of(self.orb_range_start)
        hi = self._minutes_of(self.orb_range_end)
        return lo <= t < hi

    def is_in_entry_phase(self, now_et: datetime) -> bool:
        """True during 9:45-10:30 ET (breakout entry window)."""
        t = self._time_to_minutes(now_et.time())
        lo = self._minutes_of(self.orb_entry_start)
        hi = self._minutes_of(self.orb_entry_end)
        return lo <= t < hi

    def is_in_trading_window(self, now_et: datetime) -> bool:
        """True anywhere from range start to entry end (9:30-10:30 ET)."""
        return self.is_in_range_phase(now_et) or self.is_in_entry_phase(now_et)

    def get_wind_down_time(self, now_et: datetime) -> Optional[datetime]:
        """Random wind-down start between WIND_DOWN_MIN and WIND_DOWN_MAX ET."""
        lo = self._minutes_of(self.wind_down_min)
        hi = self._minutes_of(self.wind_down_max)
        minute = self._rng.randint(lo, hi)
        hour = minute // 60
        minute = minute % 60
        return now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def is_in_wind_down(self, now_et: datetime, wind_down_start: datetime) -> bool:
        """True after wind-down started but before all-close."""
        t = self._time_to_minutes(now_et.time())
        wd = self._time_to_minutes(wind_down_start.time())
        ac = self._time_to_minutes(self._parse_hm(self.all_close))
        return wd <= t < ac

    def should_close_all(self, now_et: datetime) -> bool:
        """True at or after ALL_POSITIONS_CLOSE ET."""
        t = self._time_to_minutes(now_et.time())
        return t >= self._time_to_minutes(self._parse_hm(self.all_close))

    # ------------------------------------------------------------------
    # Day management
    # ------------------------------------------------------------------

    def _is_active_day(self, d: date) -> bool:
        """Check tradeable day AND skip-day randomness."""
        if not self.is_tradeable_day(d):
            return False
        # Decide skip for today
        if self._today != d:
            self._today = d
            self._trades_today = 0
            self._positions_closed = False
            self._skip_today = self._rng.random() < self.skip_day_chance
            if self._skip_today:
                logger.info("skip_day: decided to skip %s", d)
        return not self._skip_today

    def new_day(self, d: date) -> None:
        """Explicitly mark a new trading day (called by runner on date change)."""
        if self._today != d:
            if self._today is not None and self._trades_today > 0:
                self._trading_days_count += 1
            self._today = d
            self._trades_today = 0
            self._positions_closed = False
            self._skip_today = self._rng.random() < self.skip_day_chance

    def can_trade_now(self, now_et: datetime) -> bool:
        """True if we can still open a new position today."""
        if not self._is_active_day(now_et.date()):
            return False
        if self._trades_today >= self.daily_trade_cap:
            return False
        if self._positions_closed:
            return False
        if self.should_close_all(now_et):
            return False
        return True

    def record_trade(self) -> None:
        """Increment today's trade counter."""
        self._trades_today += 1

    def mark_positions_closed(self) -> None:
        """Mark that all positions are closed for the day."""
        self._positions_closed = True

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def trading_days_count(self) -> int:
        return self._trading_days_count

    def needs_more_trading_days(self) -> bool:
        """True if we haven't met the minimum 5 trading days yet."""
        return self._trading_days_count < self.min_trading_days
