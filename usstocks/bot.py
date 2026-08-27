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
import signal
import time
from datetime import datetime
from typing import Callable, List, Optional

import requests

from config.loader import get_env, load_config
from usstocks.data.utex_provider import UtexClient
from usstocks.dispatcher import STALE_UPDATE_SECONDS, TelegramUpdateDispatcher
from usstocks.guards import require_signal_only
from usstocks.journal import UsJournal
from usstocks.models import RiskState
from usstocks.notify import TelegramNotifier
from usstocks.premarket_ranker import ScannerConfig
from usstocks.scanner_loop import (
    SignalOnlyRunner,
    load_symbol_ids,
)
from usstocks.session import session_from_cfg
from usstocks.transport import RawTelegramTransport

logger = logging.getLogger("usstocks.bot")


class BotShutdownManager:
    """Handles SIGINT and SIGTERM for graceful shutdown of bot services."""

    def __init__(self):
        self.is_running: bool = True
        self._shutdown_callbacks: List[Callable[[], None]] = []
        self._register_signals()

    def _register_signals(self) -> None:
        def _handler(signum, frame):
            logger.info("Received termination signal %s, initiating graceful shutdown...", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            # Not in main thread or platform doesn't support signal
            pass

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        self._shutdown_callbacks.append(callback)

    def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        for cb in self._shutdown_callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Error during shutdown callback execution")


def main(shutdown_manager: Optional[BotShutdownManager] = None) -> int:
    require_signal_only("usstocks.bot")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if shutdown_manager is None:
        shutdown_manager = BotShutdownManager()

    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "us_stocks_challenge.yaml")
    cfg = load_config(cfg_path)

    today_ny = datetime.now().astimezone().date()
    state = RiskState(session_date=today_ny.isoformat())
    journal = UsJournal(cfg.get("journal", {}).get(
        "sqlite_path", "data/usstocks.sqlite"))
    journal.ensure_session(state.session_date)
    shutdown_manager.on_shutdown(lambda: journal.close())

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

    dispatcher = TelegramUpdateDispatcher()
    logger.info("usstocks.bot started: watchlist=%s signals=on", watchlist)
    while shutdown_manager.is_running:
        try:
            resp = requests.get(f"{base}/getUpdates",
                                params={"offset": offset, "timeout": 10},
                                timeout=15)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
            backoff = 5.0
        except Exception as exc:
            if not shutdown_manager.is_running:
                break
            logger.warning("poll error: %s (retry %.0fs)", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue

        now = datetime.now().astimezone()
        for upd in updates:
            next_off = dispatcher.dispatch_update(upd, controller)
            if next_off is not None:
                offset = next_off
            runner.signals_enabled = controller.signals_enabled

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

    logger.info("usstocks.bot gracefully stopped")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
