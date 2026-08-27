"""Notifier adapters (ТЗ §5 interface).

`TelegramNotifier` reuses the EXISTING alerts.telegram_bot.TelegramAlertBot
(token from env, log redaction) — no second bot is created. A pure
`PrintNotifier` covers replay/tests.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional, Protocol

from usstocks.models import RiskEvent, TradeSignal, WatchlistItem

logger = logging.getLogger("usstocks.notify")


class TelegramRateLimiter:
    """Token-bucket & sliding window rate limiter for Telegram API."""

    def __init__(self, max_per_second: float = 20.0, max_per_chat_per_second: float = 1.0,
                 throttle_window_seconds: float = 300.0, clock: Callable[[], float] = time.time):
        self.max_per_second = max_per_second
        self.max_per_chat_per_second = max_per_chat_per_second
        self.throttle_window_seconds = throttle_window_seconds
        self.clock = clock
        self._last_global_ts: float = 0.0
        self._last_chat_ts: Dict[str, float] = {}
        self._throttled_events: Dict[str, float] = {}  # key -> last_sent_ts

    def can_send(self, chat_id: str = "default") -> bool:
        now = self.clock()
        min_global_interval = 1.0 / self.max_per_second
        min_chat_interval = 1.0 / self.max_per_chat_per_second

        if now - self._last_global_ts < min_global_interval:
            return False
        if now - self._last_chat_ts.get(chat_id, 0.0) < min_chat_interval:
            return False
        return True

    def record_send(self, chat_id: str = "default") -> None:
        now = self.clock()
        self._last_global_ts = now
        self._last_chat_ts[chat_id] = now

    def should_throttle_event(self, event_key: str) -> bool:
        now = self.clock()
        last = self._throttled_events.get(event_key)
        if last is not None and (now - last) < self.throttle_window_seconds:
            return True
        self._throttled_events[event_key] = now
        return False


class Notifier(Protocol):
    def send_signal(self, signal: TradeSignal) -> None: ...
    def send_watchlist(self, watchlist: List[WatchlistItem]) -> None: ...
    def send_risk_event(self, event: RiskEvent) -> None: ...


def format_signal_message(s: TradeSignal) -> str:
    """TZ §9 template. Manual-execution disclaimer is mandatory. ASCII only."""
    side_ru = "LONG" if s.side == "long" else "SHORT"
    emoji = "[LONG]" if s.side == "long" else "[SHORT]"
    why = "\n".join(f"- {w}" for w in s.why)
    return (
        f"{emoji} US STOCKS - VWAP PULLBACK {side_ru}\n\n"
        f"Ticker: {s.symbol}\nGrade: {s.grade}\n\n"
        f"Entry: ${s.entry_low:.2f}-{s.entry_high:.2f}\n"
        f"Stop: ${s.stop:.2f}\n"
        f"Risk/share: ${s.risk_per_share:.2f}\n"
        f"Size: {s.shares} shares\n"
        f"Notional: ~${s.notional_usd:,.0f}\n"
        f"Max risk: ~${s.planned_risk_usd:.2f}\n\n"
        f"TP1: ${s.tp1:.2f} (1R)\nTP2: ${s.tp2:.2f} (2R)\n\n"
        f"Why:\n{why}\n\n"
        "[Signal-only] bot does not send orders. "
        "Check price, book and terminal manually."
    )


class TelegramNotifier:
    """Thin adapter; keeps the honesty contract of the existing bot."""

    def __init__(self, bot=None, rate_limiter: Optional[TelegramRateLimiter] = None):
        # Lazy import: tests and replay never need Telegram installed/config.
        if bot is None:
            from config.loader import load_config
            from alerts.telegram_bot import TelegramAlertBot
            bot = TelegramAlertBot(load_config())
        self._bot = bot
        self.rate_limiter = rate_limiter or TelegramRateLimiter()

    def _send(self, text: str, chat_id: str = "default", event_key: Optional[str] = None) -> None:
        if event_key and self.rate_limiter.should_throttle_event(event_key):
            logger.info("Throttling repeated telegram notification: %s", event_key)
            return

        if not self.rate_limiter.can_send(chat_id):
            logger.warning("Telegram rate limit exceeded, delaying/dropping message")
            return

        ok = False
        try:
            ok = bool(self._bot.send_text_message(text))
            if ok:
                self.rate_limiter.record_send(chat_id)
        except Exception as e:                       # never crash the scanner
            logger.error("telegram send failed: %s", e)
        if not ok:
            logger.warning("telegram not configured/delivered — message dropped")

    def send_signal(self, signal: TradeSignal) -> None:
        self._send(format_signal_message(signal), event_key=f"signal:{signal.symbol}")

    def send_watchlist(self, watchlist: List[WatchlistItem]) -> None:
        from usstocks.premarket_ranker import format_watchlist_message
        self._send(format_watchlist_message(watchlist))

    def send_risk_event(self, event: RiskEvent) -> None:
        mark = "✅" if event.allowed else "⛔"
        sym = f" [{event.symbol}]" if event.symbol else ""
        event_key = f"risk:{event.symbol or 'global'}:{event.code}" if not event.allowed else None
        self._send(f"{mark} risk{sym}: {event.code} — {event.reason}", event_key=event_key)


class PrintNotifier:
    def send_signal(self, signal: TradeSignal) -> None:
        msg = format_signal_message(signal)
        # Windows cp1252-safe: replace non-ASCII
        safe = msg.encode('ascii', 'replace').decode('ascii')
        print(safe)

    def send_watchlist(self, watchlist: List[WatchlistItem]) -> None:
        print(watchlist)

    def send_risk_event(self, event: RiskEvent) -> None:
        print(event)
