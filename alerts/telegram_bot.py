"""
Telegram bot integration - sends alerts for signals AND trade execution/close updates.
CRITICAL: bot token is read exclusively from environment variable TELEGRAM_BOT_TOKEN.
"""
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

from config.loader import get_env
from alerts.formatter import format_signal_message

logger = logging.getLogger("telegram_bot")


class TelegramAlertBot:
    def __init__(self, cfg: dict, bot_token: str = None, chat_id: str = None):
        self.cfg = cfg
        self.bot_token = bot_token or get_env("TELEGRAM_BOT_TOKEN", required=False)
        self.chat_id = chat_id or get_env("TELEGRAM_CHAT_ID", required=False)
        
        if self.bot_token:
            self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        else:
            self.base_url = None

        self._last_alert_ts: Optional[float] = None
        self._alerts_sent_today = 0
        self._current_day = datetime.now(timezone.utc).date()

    def send_text_message(self, text: str) -> bool:
        """Sends a direct custom text message to Telegram."""
        if not self.base_url or not self.chat_id:
            return False

        try:
            response = requests.post(
                self.base_url,
                data={"chat_id": self.chat_id, "text": text},
                timeout=10
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram text message: {e}")
            return False

    def _reset_daily_counter_if_needed(self):
        today = datetime.now(timezone.utc).date()
        if today != self._current_day:
            self._current_day = today
            self._alerts_sent_today = 0

    def _should_send(self, signal: dict) -> bool:
        if not self.base_url or not self.chat_id:
            return False

        alert_cfg = self.cfg.get("alerts", {})
        if signal.get("bias") == "no_trade":
            return False

        min_conf = alert_cfg.get("telegram_min_confidence", 0.50)
        if signal.get("confidence", 0.0) < min_conf:
            return False

        self._reset_daily_counter_if_needed()
        if self._alerts_sent_today >= alert_cfg.get("max_alerts_per_day", 30):
            return False

        if self._last_alert_ts is not None:
            elapsed_minutes = (time.time() - self._last_alert_ts) / 60.0
            if elapsed_minutes < alert_cfg.get("cooldown_minutes", 15):
                return False

        return True

    def send_alert_if_qualified(self, signal: dict, asset_key: str = "XAUUSD") -> bool:
        if not self._should_send(signal):
            return False

        include_meta = bool(self.cfg.get("alerts", {}).get("include_signal_meta", False))
        message = format_signal_message(signal, asset_key, include_meta=include_meta)
        success = self.send_text_message(message)
        if success:
            self._last_alert_ts = time.time()
            self._alerts_sent_today += 1
            return True
        return False
            