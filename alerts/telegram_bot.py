"""
Telegram bot integration - sends alerts for signals AND trade execution/close updates.
CRITICAL: bot token is read exclusively from environment variable TELEGRAM_BOT_TOKEN.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Optional

import requests

from alerts.formatter import format_signal_message
from config.loader import get_env

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
        self._current_day = datetime.now(UTC).date()

    def _post_with_retry(self, url: str, data: dict, timeout: float = 10, attempts: int = 3, base_delay: float = 1.5):
        """POST with a few retries on transient network errors.

        Observed live (2026-08-30): api.telegram.org connections get dropped
        with ConnectionResetError(10054) behind the VPN; a single attempt then
        silently lost the trade-close alert. Retry with exponential backoff and
        re-raise on the final attempt so callers log (redacted) and fail soft.
        """
        delay = base_delay
        for attempt in range(1, attempts + 1):
            try:
                return requests.post(url, data=data, timeout=timeout)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt == attempts:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise requests.exceptions.ConnectionError("unreachable")  # pragma: no cover

    def _redact(self, text: str) -> str:
        """Strip the bot token from a log/exception message.

        requests exception strings include the request URL, which contains the
        bot token (https://api.telegram.org/bot<TOKEN>/sendMessage). Logging that
        raw would leak the token to anyone who can read the log file, granting
        control of the /closeall control bot. Replace every occurrence of the
        token with a placeholder.
        """
        if not self.bot_token:
            return text
        return text.replace(self.bot_token, "<REDACTED>")

    def send_text_message(self, text: str) -> bool:
        """Sends a direct custom text message to Telegram."""
        if not self.base_url or not self.chat_id:
            return False

        try:
            response = self._post_with_retry(
                self.base_url, {"chat_id": self.chat_id, "text": text}, timeout=10
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram text message: {self._redact(str(e))}")
            return False

    def _reset_daily_counter_if_needed(self):
        today = datetime.now(UTC).date()
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
            published = int(time.time())
            signal["published_at_utc"] = published
            created = signal.get("timestamp_utc")
            signal["publish_latency_seconds"] = max(0, published - int(created)) if created is not None else None
            self._last_alert_ts = time.time()
            self._alerts_sent_today += 1
            return True
        return False
