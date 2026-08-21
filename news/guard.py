"""
News Guard — blocks trading around high-impact economic events.

Integrates the CalendarFeed with the trading config:
  ensemble.use_news_guard: true
  ensemble.news_buffer_before_min: 30
  ensemble.news_buffer_after_min: 30

The guard has three modes:
  - LIVE: strict, uses real-time calendar feed. On feed failure, blocks trading
    (news_feed_failure_policy_live: fail_closed).
  - BACKTEST: uses historical calendar CSV if available, otherwise no-op.
  - DISABLED: use_news_guard=false -> always clear.

Usage:
    guard = NewsGuard.from_config(cfg)
    if guard.is_blocked(asset_currency="USD"):
        # skip this signal
    status = guard.status_text()
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import os
import time
from typing import Optional

from news.calendar_feed import CalendarFeed, CalendarEvent, get_feed

logger = logging.getLogger("news.guard")

# Currency mapping: asset keys to their primary currencies
ASSET_CURRENCIES = {
    "XAUUSD": {"USD"},          # Gold priced in USD
    "XAGUSD": {"USD"},          # Silver priced in USD
    "BTCUSD": {"USD"},          # BTC priced in USD
    "EURUSD": {"EUR", "USD"},   # EUR/USD
    "GBPUSD": {"GBP", "USD"},   # GBP/USD
}

# Historical calendar CSV format:
# timestamp_utc,title,country,impact
# 2026-08-18T12:30:00,Core CPI m/m,USD,High


class NewsGuard:
    """
    Trading news guard — determines if trading should be blocked due to
    upcoming high-impact economic events.
    """

    def __init__(
        self,
        enabled: bool = True,
        buffer_before_min: int = 30,
        buffer_after_min: int = 30,
        failure_policy: str = "fail_closed",
        feed: Optional[CalendarFeed] = None,
        historical_csv_path: Optional[str] = None,
    ) -> None:
        self.enabled = enabled
        self.buffer_before_min = buffer_before_min
        self.buffer_after_min = buffer_after_min
        self.failure_policy = failure_policy  # "fail_closed" | "fail_open"
        self._feed = feed or get_feed()
        self._historical_events: list[CalendarEvent] = []
        self._feed_ok: Optional[bool] = None  # None = not tested yet

        if historical_csv_path:
            self._load_historical(historical_csv_path)

    @classmethod
    def from_config(cls, cfg: dict) -> "NewsGuard":
        """Create a NewsGuard from the trading config dict."""
        ens = cfg.get("ensemble", {})
        return cls(
            enabled=ens.get("use_news_guard", False),
            buffer_before_min=ens.get("news_buffer_before_min", 30),
            buffer_after_min=ens.get("news_buffer_after_min", 30),
            failure_policy=ens.get("news_feed_failure_policy_live", "fail_closed"),
            historical_csv_path=ens.get("historical_news_calendar_path"),
        )

    def _load_historical(self, path: str) -> None:
        """Load historical calendar from CSV."""
        if not path or not os.path.exists(path):
            logger.info("No historical calendar CSV at %s", path)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        dt_utc = dt.datetime.fromisoformat(row["timestamp_utc"])
                        self._historical_events.append(CalendarEvent(
                            title=row.get("title", ""),
                            currency=row.get("country", "").upper(),
                            datetime_utc=dt_utc,
                            impact=row.get("impact", "Low").capitalize(),
                        ))
                    except (ValueError, KeyError):
                        continue
            self._historical_events.sort(key=lambda e: e.datetime_utc)
            logger.info("Loaded %d historical calendar events from %s",
                        len(self._historical_events), path)
        except Exception as e:
            logger.warning("Failed to load historical calendar: %s", e)

    def _try_refresh_feed(self) -> bool:
        """Try to refresh the feed; return True if successful."""
        try:
            self._feed._ensure_cache()
            self._feed_ok = True
            return True
        except Exception as e:
            logger.warning("Calendar feed refresh failed: %s", e)
            self._feed_ok = False
            return False

    def is_blocked(
        self,
        now: Optional[dt.datetime] = None,
        asset_key: Optional[str] = None,
    ) -> bool:
        """
        Check if trading should be blocked at `now` for the given asset.

        Args:
            now: Current UTC time. Default: utcnow().
            asset_key: Asset key (e.g. "XAUUSD"). If set, only checks events
                      relevant to the asset's currencies.

        Returns:
            True if trading should be blocked (red zone).
        """
        if not self.enabled:
            return False

        now = now or dt.datetime.utcnow()
        currencies = ASSET_CURRENCIES.get(asset_key) if asset_key else None

        # Check historical events first (for backtesting)
        if self._historical_events:
            if self._check_events(now, self._historical_events, currencies):
                return True

        # Check live feed
        try:
            blocked = self._feed.is_red_zone(
                now=now,
                buffer_min=max(self.buffer_before_min, self.buffer_after_min),
                currencies=currencies,
            )
            self._feed_ok = True
            return blocked
        except Exception as e:
            logger.warning("Feed check failed: %s", e)
            self._feed_ok = False

            # Feed failure policy
            if self.failure_policy == "fail_closed":
                logger.warning("News feed FAILED — blocking trading (fail_closed)")
                return True
            else:
                # fail_open: allow trading even without news data
                return False

    def _check_events(
        self,
        now: dt.datetime,
        events: list[CalendarEvent],
        currencies: Optional[set[str]],
    ) -> bool:
        """Check if now is within buffer of any HIGH event in the list."""
        window_start = now - dt.timedelta(hours=24)
        window_end = now + dt.timedelta(hours=24)

        for ev in events:
            if not ev.is_high:
                continue
            if currencies and ev.currency not in currencies:
                continue
            if window_start <= ev.datetime_utc <= window_end:
                diff_sec = (ev.datetime_utc - now).total_seconds()
                # Before the event: check buffer_before
                if -self.buffer_after_min * 60 <= diff_sec <= self.buffer_before_min * 60:
                    return True
        return False

    def next_event(self, asset_key: Optional[str] = None) -> Optional[CalendarEvent]:
        """Get the next HIGH impact event relevant to the asset."""
        currencies = ASSET_CURRENCIES.get(asset_key) if asset_key else None
        try:
            return self._feed.next_high_impact(currencies=currencies)
        except Exception:
            return None

    def status_text(self, asset_key: Optional[str] = None) -> str:
        """Generate a human-readable status of the news guard."""
        if not self.enabled:
            return "📰 News Guard: DISABLED"

        now = dt.datetime.utcnow()
        blocked = self.is_blocked(now, asset_key)
        next_h = self.next_event(asset_key)

        status = f"📰 News Guard: {'🔴 BLOCKED' if blocked else '🟢 CLEAR'}\n"
        status += f"  Buffer: ±{self.buffer_before_min} min\n"

        if next_h:
            diff_h = (next_h.datetime_utc - now).total_seconds() / 3600
            if diff_h > 0:
                status += f"  Next HIGH: {next_h.currency} {next_h.title}\n"
                status += f"    in {diff_h:.1f}h ({next_h.datetime_utc:%Y-%m-%d %H:%M UTC})\n"
            else:
                status += f"  Last HIGH: {next_h.currency} {next_h.title} ({abs(diff_h):.1f}h ago)\n"
        else:
            status += "  Next HIGH: none upcoming\n"

        feed_status = "OK" if self._feed_ok else ("FAIL" if self._feed_ok is False else "unchecked")
        status += f"  Feed: {feed_status}"

        return status


# Module-level singleton
_guard: Optional[NewsGuard] = None
_guard_lock = __import__("threading").Lock()


def get_guard(cfg: Optional[dict] = None) -> NewsGuard:
    """Get or create the singleton NewsGuard instance."""
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                if cfg:
                    _guard = NewsGuard.from_config(cfg)
                else:
                    _guard = NewsGuard(enabled=False)
    return _guard
