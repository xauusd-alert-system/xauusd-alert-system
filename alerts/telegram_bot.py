"""
Telegram bot integration - sends alerts ONLY for signals at/above the configured
confidence threshold (config.yaml alerts.telegram_min_confidence).

CRITICAL: bot token is read exclusively from environment variable TELEGRAM_BOT_TOKEN,
never hardcoded (see config/loader.py::get_env). Chat ID is likewise env-based.

Cooldown and max-alerts-per-day logic is enforced here to satisfy the project's
"fewer, higher-confidence alerts" design goal - even a correctly high-confidence
signal is suppressed if it arrives within the cooldown window or exceeds the daily cap.
"""
import time
import requests
from datetime import datetime, timezone
from typing import Optional

from config.loader import get_env
from alerts.formatter import format_signal_message


class TelegramAlertBot:
    def __init__(self, cfg: dict, bot_token: str = None, chat_id: str = None):
        self.cfg = cfg
        self.bot_token = bot_token or get_env("TELEGRAM_BOT_TOKEN", required=True)
        self.chat_id = chat_id or get_env("TELEGRAM_CHAT_ID", required=True)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        self._last_alert_ts: Optional[float] = None
        self._alerts_sent_today = 0
        self._current_day = datetime.now(timezone.utc).date()

    def _reset_daily_counter_if_needed(self):
        today = datetime.now(timezone.utc).date()
        if today != self._current_day:
            self._current_day = today
            self._alerts_sent_today = 0

    def _should_send(self, signal: dict) -> bool:
        """Gate on confidence threshold, cooldown, and daily cap - all from config.yaml."""
        alert_cfg = self.cfg["alerts"]

        if signal["bias"] == "no_trade":
            return False
        if signal["confidence"] < alert_cfg["telegram_min_confidence"]:
            return False

        self._reset_daily_counter_if_needed()
        if self._alerts_sent_today >= alert_cfg["max_alerts_per_day"]:
            return False

        if self._last_alert_ts is not None:
            elapsed_minutes = (time.time() - self._last_alert_ts) / 60.0
            if elapsed_minutes < alert_cfg["cooldown_minutes"]:
                return False

        return True

    def send_alert_if_qualified(self, signal: dict) -> bool:
        """
        Returns True if an alert was actually sent, False if suppressed.
        Never raises on suppression - suppression is expected, normal behavior.
        """
        if not self._should_send(signal):
            return False

        message = format_signal_message(signal)
        response = requests.post(self.base_url, data={"chat_id": self.chat_id, "text": message}, timeout=10)
        response.raise_for_status()

        self._last_alert_ts = time.time()
        self._alerts_sent_today += 1
        return True
