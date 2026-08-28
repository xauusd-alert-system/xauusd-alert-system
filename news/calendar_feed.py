"""
Economic Calendar Feed — fetches high-impact events from ForexFactory.

Free API: nfs.faireconomy.media/ff_calendar_<week>.json
Returns structured events with: title, country (currency), date (UTC), impact (Low/Medium/High),
forecast, previous.

The feed is cached in-memory for 1 hour to avoid hammering the API on every poll.
Fetched data covers the current week (Mon-Sun). The guard must fetch next-week data
separately when checking events beyond Sunday.

Usage:
    feed = CalendarFeed()
    events = feed.get_upcoming(hours=48)       # all events in next 48h
    high = feed.get_high_impact(hours=48)       # HIGH only
    is_red = feed.is_red_zone(utc_now, buffer_min=30)  # True if within ±buffer of HIGH
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("news.calendar_feed")

# ForexFactory free JSON endpoint (no API key required)
_FF_BASE = "https://nfs.faireconomy.media/ff_calendar_{week}.json"
_WEEKS = ["lastweek", "thisweek", "nextweek"]

# In-memory cache TTL (seconds)
_CACHE_TTL = 3600  # 1 hour
# Disk cache TTL — allow stale cache for 24h if API fails
_DISK_CACHE_TTL = 86400  # 24 hours


@dataclass
class CalendarEvent:
    """Single economic calendar event."""
    title: str
    currency: str           # e.g. "USD", "EUR", "GBP"
    datetime_utc: dt.datetime
    impact: str             # "Low" | "Medium" | "High"
    forecast: str = ""
    previous: str = ""

    @property
    def is_high(self) -> bool:
        return self.impact == "High"

    @property
    def is_medium_or_high(self) -> bool:
        return self.impact in ("Medium", "High")

    def __repr__(self) -> str:
        return (f"CalendarEvent({self.impact} {self.currency} {self.title} "
                f"@ {self.datetime_utc:%Y-%m-%d %H:%M})")


def _parse_event(raw: dict) -> Optional[CalendarEvent]:
    """Parse a raw ForexFactory event dict into a CalendarEvent."""
    try:
        title = raw.get("title", "").strip()
        currency = raw.get("country", "").strip().upper()
        impact = raw.get("impact", "Low").strip().capitalize()
        date_str = raw.get("date", "")
        forecast = raw.get("forecast", "") or ""
        previous = raw.get("previous", "") or ""

        if not title or not date_str:
            return None

        # Parse ISO datetime with timezone offset
        # Format: "2026-08-18T08:30:00-04:00"
        dt_obj = dt.datetime.fromisoformat(date_str)
        # Convert to UTC
        dt_utc = dt_obj.astimezone(dt.UTC).replace(tzinfo=None)

        return CalendarEvent(
            title=title,
            currency=currency,
            datetime_utc=dt_utc,
            impact=impact,
            forecast=forecast,
            previous=previous,
        )
    except Exception:
        return None


class CalendarFeed:
    """
    Economic calendar feed with in-memory caching.

    Fetches this week + next week on first access. Cached for 1 hour.
    Thread-safe for concurrent reads from the trading thread and Telegram bot.
    """

    def __init__(self) -> None:
        self._cache: list[CalendarEvent] = []
        self._cache_ts: float = 0.0
        self._lock = threading.Lock()
        # Disk cache to avoid API rate limits
        self._disk_cache_path = os.path.join("data", "news_calendar_cache.json")
        self._load_disk_cache()

    def _fetch_week(self, week: str, retries: int = 3) -> list[CalendarEvent]:
        """Fetch events for one week from faireconomy.media (FF mirror)."""
        url = _FF_BASE.format(week=week)
        for attempt in range(retries):
            try:
                resp = requests.get(url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; NewsGuard/1.0)",
                })
                if resp.status_code == 429:
                    wait = 2 ** attempt * 2  # 2s, 4s, 8s
                    logger.warning("Rate limited on %s, waiting %ds", week, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                raw = resp.json()
                events = []
                for item in raw:
                    ev = _parse_event(item)
                    if ev is not None:
                        events.append(ev)
                logger.info("Fetched %d events from %s", len(events), week)
                return events
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.info("No data for %s (404)", week)
                    return []
                logger.warning("Failed to fetch calendar for %s: %s", week, e)
                return []
            except Exception as e:
                logger.warning("Failed to fetch calendar for %s: %s", week, e)
                return []
        return []

    def _refresh(self) -> None:
        """Refresh the cache from the API."""
        now = time.time()
        all_events: list[CalendarEvent] = []

        # Fetch this week and next week (covers ~14 days ahead)
        for week in ["thisweek", "nextweek"]:
            all_events.extend(self._fetch_week(week))

        # Sort by datetime
        all_events.sort(key=lambda e: e.datetime_utc)

        # Deduplicate by (title, currency, datetime)
        seen = set()
        deduped = []
        for ev in all_events:
            key = (ev.title, ev.currency, ev.datetime_utc.isoformat())
            if key not in seen:
                seen.add(key)
                deduped.append(ev)

        self._cache = deduped
        self._cache_ts = now
        logger.info("Calendar feed refreshed: %d events (HIGH: %d, MEDIUM: %d)",
                     len(deduped),
                     sum(1 for e in deduped if e.is_high),
                     sum(1 for e in deduped if e.impact == "Medium"))

    def _load_disk_cache(self) -> None:
        """Load events from disk cache if fresh enough."""
        try:
            if not os.path.exists(self._disk_cache_path):
                return
            with open(self._disk_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache_ts = data.get("ts", 0)
            age = time.time() - cache_ts
            if age > _DISK_CACHE_TTL:
                logger.warning("Disk cache too old (%.0fh), ignoring", age / 3600)
                return
            events = []
            for item in data.get("events", []):
                ev = _parse_event(item)
                if ev is not None:
                    events.append(ev)
            if events:
                self._cache = events
                self._cache_ts = cache_ts
                if age > _CACHE_TTL:
                    logger.info("Loaded stale disk cache (%.0fh old, %d events)",
                                age / 3600, len(events))
        except Exception:
            pass  # corrupt cache, ignore

    def _save_disk_cache(self) -> None:
        """Persist current cache to disk."""
        try:
            os.makedirs(os.path.dirname(self._disk_cache_path), exist_ok=True)
            events_data = []
            for ev in self._cache:
                events_data.append({
                    "title": ev.title,
                    "country": ev.currency,
                    "date": ev.datetime_utc.isoformat() + "+00:00",
                    "impact": ev.impact,
                    "forecast": ev.forecast,
                    "previous": ev.previous,
                })
            with open(self._disk_cache_path, "w", encoding="utf-8") as f:
                json.dump({"ts": self._cache_ts, "events": events_data}, f)
        except Exception:
            pass

    def _ensure_cache(self) -> None:
        """Ensure the cache is fresh (within TTL)."""
        now = time.time()
        if now - self._cache_ts > _CACHE_TTL or not self._cache:
            with self._lock:
                # Double-check after acquiring lock
                if now - self._cache_ts > _CACHE_TTL or not self._cache:
                    self._refresh()
                    if self._cache:  # only save if refresh succeeded
                        self._save_disk_cache()
                    # If refresh failed (empty cache), try to restore from disk
                    if not self._cache:
                        self._load_disk_cache()

    def get_all(self) -> list[CalendarEvent]:
        """Get all cached events (sorted by datetime)."""
        self._ensure_cache()
        return list(self._cache)

    def get_upcoming(self, hours: float = 48.0, reference: Optional[dt.datetime] = None) -> list[CalendarEvent]:
        """Get events within the next `hours` from now (or `reference`)."""
        self._ensure_cache()
        now = reference or dt.datetime.utcnow()
        cutoff = now + dt.timedelta(hours=hours)
        return [e for e in self._cache if now <= e.datetime_utc <= cutoff]

    def get_high_impact(self, hours: float = 48.0, reference: Optional[dt.datetime] = None) -> list[CalendarEvent]:
        """Get HIGH impact events within next `hours`."""
        return [e for e in self.get_upcoming(hours, reference) if e.is_high]

    def is_red_zone(self, now: Optional[dt.datetime] = None,
                    buffer_min: int = 30,
                    currencies: Optional[set[str]] = None) -> bool:
        """
        Check if `now` is within ±buffer_min of a HIGH impact event.

        Args:
            now: Current UTC time (default: utcnow).
            buffer_min: Buffer in minutes around each HIGH event.
            currencies: If set, only check events for these currencies.
                        None = check all.

        Returns:
            True if we're in a red zone (should not trade).
        """
        self._ensure_cache()
        now = now or dt.datetime.utcnow()

        # Check ±24h window (we only care about near-term events)
        window_start = now - dt.timedelta(hours=24)
        window_end = now + dt.timedelta(hours=24)

        for ev in self._cache:
            if not ev.is_high:
                continue
            if currencies and ev.currency not in currencies:
                continue
            if window_start <= ev.datetime_utc <= window_end:
                diff = abs((ev.datetime_utc - now).total_seconds())
                if diff <= buffer_min * 60:
                    return True
        return False

    def next_high_impact(self, reference: Optional[dt.datetime] = None,
                         currencies: Optional[set[str]] = None) -> Optional[CalendarEvent]:
        """Return the next upcoming HIGH impact event, or None."""
        self._ensure_cache()
        now = reference or dt.datetime.utcnow()
        for ev in self._cache:
            if not ev.is_high:
                continue
            if currencies and ev.currency not in currencies:
                continue
            if ev.datetime_utc > now:
                return ev
        return None

    def format_upcoming(self, hours: float = 48.0, max_events: int = 15,
                         reference: Optional[dt.datetime] = None) -> str:
        """Format upcoming events as a readable string for Telegram."""
        events = self.get_upcoming(hours, reference=reference)
        if not events:
            return "📭 Нет запланированных событий на ближайшие %.0f ч." % hours

        # Group by date
        by_date: dict[str, list[CalendarEvent]] = {}
        for ev in events:
            day_key = ev.datetime_utc.strftime("%a %d %b")
            by_date.setdefault(day_key, []).append(ev)

        lines = ["📰 *Economic Calendar* (ближайшие %.0f ч):\n" % hours]
        count = 0
        for day, day_events in by_date.items():
            lines.append(f"📅 *{day}*")
            for ev in day_events:
                if count >= max_events:
                    lines.append(f"  ...и ещё {len(events) - max_events}")
                    return "\n".join(lines)
                # Impact icon
                icon = "🔴" if ev.is_high else ("🟡" if ev.impact == "Medium" else "⚪")
                time_str = ev.datetime_utc.strftime("%H:%M")
                forecast_str = f" → {ev.forecast}" if ev.forecast else ""
                prev_str = f" (prev: {ev.previous})" if ev.previous else ""
                lines.append(f"  {icon} {time_str} UTC {ev.currency}: {ev.title}{forecast_str}{prev_str}")
                count += 1
            lines.append("")

        return "\n".join(lines)

    def format_status(self) -> str:
        """Format feed status for debugging."""
        self._ensure_cache()
        high = sum(1 for e in self._cache if e.is_high)
        med = sum(1 for e in self._cache if e.impact == "Medium")
        low = sum(1 for e in self._cache if e.impact == "Low")
        now = dt.datetime.utcnow()
        next_h = self.next_high_impact()
        age_min = (time.time() - self._cache_ts) / 60

        status = (
            f"📰 Calendar Feed Status:\n"
            f"  Events: {len(self._cache)} total ({high} HIGH, {med} MED, {low} LOW)\n"
            f"  Cache age: {age_min:.0f} min\n"
        )
        if next_h:
            diff = (next_h.datetime_utc - now).total_seconds() / 3600
            status += f"  Next HIGH: {next_h.currency} {next_h.title} in {diff:.1f}h\n"
        else:
            status += "  Next HIGH: none in upcoming events\n"

        return status


# Module-level singleton
_feed: Optional[CalendarFeed] = None
_feed_lock = threading.Lock()


def get_feed() -> CalendarFeed:
    """Get or create the singleton CalendarFeed instance."""
    global _feed
    if _feed is None:
        with _feed_lock:
            if _feed is None:
                _feed = CalendarFeed()
    return _feed
