"""Telegram Update Dispatcher for usstocks bot (P2-5)."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("usstocks.dispatcher")

STALE_UPDATE_SECONDS = 600


class TelegramUpdateDispatcher:
    """Parses raw getUpdates items and dispatches to UsCommandsController."""

    def __init__(self, stale_timeout_seconds: float = STALE_UPDATE_SECONDS):
        self.stale_timeout = stale_timeout_seconds

    def dispatch_update(self, upd: Dict[str, Any], controller: Any) -> Optional[int]:
        """Dispatch a single Telegram update dictionary. Returns next update_id offset or None."""
        update_id = upd.get("update_id")
        next_offset = (update_id + 1) if update_id is not None else None

        msg = upd.get("message") or upd.get("edited_message")
        cb = upd.get("callback_query")

        try:
            if msg and msg.get("text", "").startswith("/"):
                date_ts = msg.get("date")
                if date_ts and time.time() - int(date_ts) > self.stale_timeout:
                    logger.debug("Skipping stale message: %s", msg.get("text"))
                    return next_offset

                parts = msg["text"].strip().split()
                cmd = parts[0].lower().split("@")[0]
                controller.handle_command(cmd, str(msg["chat"]["id"]), tuple(parts[1:]))

            elif cb:
                data = cb.get("data", "")
                chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
                controller.handle_callback(data, chat_id, callback_id=cb.get("id"))

        except Exception:
            logger.exception("Update dispatch failed for update_id %s", update_id)

        return next_offset
