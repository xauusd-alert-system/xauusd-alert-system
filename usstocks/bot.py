"""usstocks.bot — ONE process for the us_stocks_challenge profile.

Owns the single getUpdates consumer for the bot token (the legacy control
bot cannot run here — scripts.run_bot refuses to start under signal-only
profiles) and alternates between Telegram updates and scanner cycles:

    python -m usstocks.bot

- /us_* commands via alerts.us_commands.UsCommandsController;
- mutations of P&L/risk-state require inline ✅ (ТЗ §12.16);
- scan cycles run only inside the NY regular session, honoring
  /us_signals on|off and the operator's stop-day.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import List, Optional

import requests

from config.loader import get_env, load_config
from usstocks.data.utex_provider import UtexClient
from usstocks.guards import require_signal_only
from usstocks.journal import UsJournal
from usstocks.models import RiskState
from usstocks.notify import TelegramNotifier
from usstocks.scanner_loop import (
    SignalOnlyRunner,
    load_symbol_ids,
)
from usstocks.premarket_ranker import ScannerConfig
from usstocks.session import session_from_cfg

logger = logging.getLogger("usstocks.bot")

STALE_UPDATE_SECONDS = 600          # same replay guard as alerts/control_bot.py


class RawTelegramTransport:
    """Minimal sendMessage/answerCallbackQuery/sendDocument client."""

    def __init__(self, token: str):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for usstocks.bot")
        self._base = f"https://api.telegram.org/bot{token}"

    def send(self, chat_id: str, text: str, reply_markup: Optional[dict] = None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            import json as _json
            payload["reply_markup"] = _json.dumps(reply_markup)
            payload["parse_mode"] = "HTML"
        try:
            requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        except Exception:
            logger.exception("sendMessage failed")

    def answer_callback(self, callback_query_id: str):
        try:
            requests.post(f"{self._base}/answerCallbackQuery",
                          json={"callback_query_id": callback_query_id},
                          timeout=10)
        except Exception:
            logger.exception("answerCallbackQuery failed")

    def send_document(self, chat_id: str, path: str):
        try:
            with open(path, "rb") as f:
                requests.post(f"{self._base}/sendDocument",
                              data={"chat_id": chat_id},
                              files={"document": f}, timeout=30)
        except Exception:
            logger.exception("sendDocument failed")


def main() -> int:
    require_signal_only("usstocks.bot")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "us_stocks_challenge.yaml")
    cfg = load_config(cfg_path)

    today_ny = datetime.now().astimezone().date()
    state = RiskState(session_date=today_ny.isoformat())
    journal = UsJournal(cfg.get("journal", {}).get(
        "sqlite_path", "data/usstocks.sqlite"))
    journal.ensure_session(state.session_date)

    client = UtexClient()
    symbol_ids = load_symbol_ids()

    class Provider:
        @staticmethod
        def get_bars(symbol: str, count: int) -> List:
            sid = symbol_ids.get(symbol.upper())
            if not sid:
                raise KeyError(f"{symbol}: no symbolId mapping")
            access = client.refresh_access()
            return client.fetch_bars(access, sid, candles_count=count)

    scfg = ScannerConfig.from_cfg(cfg)
    env_watchlist = os.getenv("US_WATCHLIST", "")
    watchlist = ([s.strip().upper() for s in env_watchlist.split(",") if s.strip()]
                 or cfg.get("us_stocks", {}).get("base_universe",
                                                 [])[:scfg.max_watchlist_size])
    runner = SignalOnlyRunner(cfg, Provider(), TelegramNotifier(),
                              watchlist=watchlist, state=state,
                              journal=journal, symbol_ids=symbol_ids)

    transport = RawTelegramTransport(get_env("TELEGRAM_BOT_TOKEN", required=True))
    admin_id = str(get_env("TELEGRAM_ADMIN_CHAT_ID", required=False)
                   or get_env("TELEGRAM_CHAT_ID", required=False) or "")
    from alerts.us_commands import UsCommandsController
    controller = UsCommandsController(journal=journal, state=state,
                                      admin_id=admin_id, transport=transport)

    session = session_from_cfg(cfg)
    poll_seconds = float(cfg.get("scanner", {}).get("poll_seconds", 60))
    last_scan = 0.0
    offset = 0
    base = transport._base
    backoff = 5.0

    logger.info("usstocks.bot started: watchlist=%s signals=on", watchlist)
    while True:
        try:
            resp = requests.get(f"{base}/getUpdates",
                                params={"offset": offset, "timeout": 10},
                                timeout=15)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
            backoff = 5.0
        except Exception as exc:
            logger.warning("poll error: %s (retry %.0fs)", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue

        now = datetime.now().astimezone()
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            cb = upd.get("callback_query")
            try:
                if msg and msg.get("text", "").startswith("/"):
                    date_ts = msg.get("date")
                    if date_ts and time.time() - int(date_ts) > STALE_UPDATE_SECONDS:
                        continue                      # crash-restart replay guard
                    parts = msg["text"].strip().split()
                    cmd = parts[0].lower().split("@")[0]
                    controller.handle_command(cmd, str(msg["chat"]["id"]),
                                              tuple(parts[1:]))
                elif cb:
                    data = cb.get("data", "")
                    chat_id = str((cb.get("message") or {})
                                  .get("chat", {}).get("id", ""))
                    controller.handle_callback(data, chat_id,
                                               callback_id=cb.get("id"))
                runner.signals_enabled = controller.signals_enabled
            except Exception:
                logger.exception("update handling failed")

        # Scanner cadence: only during the NY regular session of a trading day.
        runner.signals_enabled = controller.signals_enabled
        now = datetime.now().astimezone()
        in_window = (
            session.is_trading_day(now.date())
            and session.session_open(now.date())
            <= now <= session.session_close(now.date()))
        if (in_window and controller.signals_enabled
                and not state.day_stopped
                and time.time() - last_scan >= poll_seconds):
            last_scan = time.time()
            try:
                runner.scan_once(now)
            except Exception:
                logger.exception("scan cycle failed")


if __name__ == "__main__":
    import sys
    sys.exit(main())
