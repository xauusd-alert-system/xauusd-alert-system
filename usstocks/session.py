"""NYSE session math in America/New_York — DST-correct, config-driven.

All functions accept/return timezone-aware datetimes. Holidays come from the
profile config (config/us_stocks_challenge.yaml), not from code.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def ensure_ny(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=NY)
    return ts.astimezone(NY)


def parse_holidays(raw: Iterable) -> List[date]:
    """Config entries may be `YYYY-MM-DD` strings or datetime.date."""
    out: List[date] = []
    for item in raw or []:
        if isinstance(item, date):
            out.append(item)
        else:
            out.append(date.fromisoformat(str(item)[:10]))
    return out


class NySession:
    """Trading-day calendar for one market (weekend + configured holidays)."""

    def __init__(self, holidays: Optional[Iterable] = None,
                 open_at: time = REGULAR_OPEN, close_at: time = REGULAR_CLOSE):
        self.holidays = set(parse_holidays(holidays))
        self.open_at = open_at
        self.close_at = close_at

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, d: date, limit: int = 30) -> Optional[date]:
        for i in range(1, limit + 1):
            cand = d + timedelta(days=i)
            if self.is_trading_day(cand):
                return cand
        return None

    def session_open(self, d: date) -> datetime:
        return datetime.combine(d, self.open_at, tzinfo=NY)

    def session_close(self, d: date) -> datetime:
        return datetime.combine(d, self.close_at, tzinfo=NY)

    def minutes_to_close(self, now: datetime) -> float:
        now = ensure_ny(now)
        close = self.session_close(now.date())
        if now > close:                       # after the close -> next session
            nxt = self.next_trading_day(now.date())
            close = self.session_close(nxt) if nxt else close
        return (close - now).total_seconds() / 60.0


# 2026 US market holidays (half-days ignored: no NEW entries late anyway).
US_MARKET_HOLIDAYS_2026 = [
    "2026-01-01",   # New Year's Day
    "2026-01-19",   # MLK Jr. Day
    "2026-02-16",   # Presidents' Day
    "2026-04-03",   # Good Friday
    "2026-05-25",   # Memorial Day
    "2026-06-19",   # Juneteenth
    "2026-07-03",   # Independence Day (observed)
    "2026-09-07",   # Labor Day
    "2026-11-26",   # Thanksgiving
    "2026-12-25",   # Christmas
]


def session_from_cfg(cfg: dict) -> NySession:
    sess = (cfg or {}).get("session", {})
    return NySession(
        holidays=sess.get("holidays", US_MARKET_HOLIDAYS_2026),
        open_at=_parse_hm(sess.get("regular_open", "09:30")),
        close_at=_parse_hm(sess.get("regular_close", "16:00")),
    )


def _parse_hm(hm: str) -> time:
    h, m = str(hm).split(":")[:2]
    return time(int(h), int(m))
