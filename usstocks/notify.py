"""Notifier adapters (ТЗ §5 interface).

`TelegramNotifier` reuses the EXISTING alerts.telegram_bot.TelegramAlertBot
(token from env, log redaction) — no second bot is created. A pure
`PrintNotifier` covers replay/tests.
"""
from __future__ import annotations

import logging
from typing import List, Protocol

from usstocks.models import RiskEvent, TradeSignal, WatchlistItem

logger = logging.getLogger("usstocks.notify")


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

    def __init__(self, bot=None):
        # Lazy import: tests and replay never need Telegram installed/config.
        if bot is None:
            from config.loader import load_config
            from alerts.telegram_bot import TelegramAlertBot
            bot = TelegramAlertBot(load_config())
        self._bot = bot

    def _send(self, text: str) -> None:
        ok = False
        try:
            ok = bool(self._bot.send_text_message(text))
        except Exception as e:                       # never crash the scanner
            logger.error("telegram send failed: %s", e)
        if not ok:
            logger.warning("telegram not configured/delivered — message dropped")

    def send_signal(self, signal: TradeSignal) -> None:
        self._send(format_signal_message(signal))

    def send_watchlist(self, watchlist: List[WatchlistItem]) -> None:
        from usstocks.premarket_ranker import format_watchlist_message
        self._send(format_watchlist_message(watchlist))

    def send_risk_event(self, event: RiskEvent) -> None:
        mark = "✅" if event.allowed else "⛔"
        sym = f" [{event.symbol}]" if event.symbol else ""
        self._send(f"{mark} risk{sym}: {event.code} — {event.reason}")


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
