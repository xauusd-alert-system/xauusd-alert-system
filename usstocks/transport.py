"""Raw HTTP Telegram Transport for usstocks bot (P2-5)."""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

logger = logging.getLogger("usstocks.transport")


class RawTelegramTransport:
    """Minimal sendMessage/answerCallbackQuery/sendDocument client."""

    def __init__(self, token: str):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for usstocks.bot")
        self.token = token
        self._base = f"https://api.telegram.org/bot{token}"

    def send(self, chat_id: str, text: str, reply_markup: Optional[dict] = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
            payload["parse_mode"] = "HTML"
        try:
            requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        except Exception:
            logger.exception("sendMessage failed")

    def answer_callback(self, callback_query_id: str) -> None:
        try:
            requests.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
                timeout=10,
            )
        except Exception:
            logger.exception("answerCallbackQuery failed")

    def send_document(self, chat_id: str, path: str) -> None:
        try:
            with open(path, "rb") as f:
                requests.post(
                    f"{self._base}/sendDocument",
                    data={"chat_id": chat_id},
                    files={"document": f},
                    timeout=30,
                )
        except Exception:
            logger.exception("sendDocument failed")
