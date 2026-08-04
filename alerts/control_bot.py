"""
Telegram Control Bot — interactive panel to manage the running trader.

Runs in a background thread via long-polling. All mutating commands
(/pause, /resume, /closeall) are restricted to TELEGRAM_ADMIN_CHAT_ID
so random users cannot control the bot.

Commands
--------
/start   — welcome message
/help    — list all commands
/status  — account equity, balance, open positions count, paused flag
/positions — list every open position with entry, current price, PnL
/pause   — set trader.dry_run = True (orders logged, not sent)
/resume  — set trader.dry_run = False
/closeall — market-close every open position (emergency stop)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, TYPE_CHECKING

import requests

from config.loader import get_env

if TYPE_CHECKING:
    from execution.mt5_trader import MultiAssetMT5Trader

logger = logging.getLogger("control_bot")


class TelegramControlBot:
    """Long-polling Telegram bot that controls a running MultiAssetMT5Trader."""

    POLL_TIMEOUT = 30          # seconds for long-poll
    RETRY_SLEEP  = 5           # seconds between retries on network error

    def __init__(self, trader: "MultiAssetMT5Trader") -> None:
        self.trader = trader
        self.token: str = get_env("TELEGRAM_BOT_TOKEN", required=True)
        # Admin chat ID: only this chat can issue mutating commands.
        self.admin_id: str = str(
            get_env("TELEGRAM_ADMIN_CHAT_ID", required=False)
            or get_env("TELEGRAM_CHAT_ID", required=False)
            or ""
        )
        self._base = f"https://api.telegram.org/bot{self.token}"
        self._offset: int = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Launch the polling loop in a daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="tg-control-bot",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram control bot started (admin_id=%s)", self.admin_id or "<any>")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self._get_updates()
                for upd in updates:
                    self._handle_update(upd)
            except Exception as exc:
                logger.warning("Poll error: %s", exc)
                time.sleep(self.RETRY_SLEEP)

    def _get_updates(self) -> list:
        resp = requests.get(
            f"{self._base}/getUpdates",
            params={"offset": self._offset, "timeout": self.POLL_TIMEOUT},
            timeout=self.POLL_TIMEOUT + 5,
        )
        resp.raise_for_status()
        data = resp.json()
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        cmd = text.split()[0].lower().split("@")[0]   # strip /cmd@botname
        logger.info("Command '%s' from chat_id=%s", cmd, chat_id)
        self._dispatch(cmd, chat_id)

    # ------------------------------------------------------------------
    # Command dispatcher
    # ------------------------------------------------------------------
    def _dispatch(self, cmd: str, chat_id: str) -> None:
        handlers = {
            "/start":     self._cmd_start,
            "/help":      self._cmd_help,
            "/status":    self._cmd_status,
            "/positions": self._cmd_positions,
            "/pause":     self._cmd_pause,
            "/resume":    self._cmd_resume,
            "/closeall":  self._cmd_closeall,
        }
        fn = handlers.get(cmd)
        if fn is None:
            self._send(chat_id, "❓ Unknown command. Type /help for the list.")
            return
        # Mutating commands require admin auth
        if cmd in ("/pause", "/resume", "/closeall") and not self._is_admin(chat_id):
            self._send(chat_id, "⛔ Unauthorised. This command is restricted to the bot owner.")
            return
        fn(chat_id)

    def _is_admin(self, chat_id: str) -> bool:
        # HIGH 34: fail-closed. If no admin id is configured, mutating commands
        # (like /closeall, /pause, /resume) must NOT be allowed for anyone.
        # Allow-all here is a dangerous fail-open that would let any Telegram
        # user shut down or close all positions of the live trader.
        if not self.admin_id:
            logger.warning(
                "Telegram admin not configured (TELEGRAM_ADMIN_CHAT_ID/TELEGRAM_CHAT_ID "
                "empty) - refusing mutating command from chat_id=%s", chat_id
            )
            return False
        return chat_id == self.admin_id

    # ------------------------------------------------------------------
    # Individual command handlers
    # ------------------------------------------------------------------
    def _cmd_start(self, chat_id: str) -> None:
        self._send(chat_id,
            "🤖 *XAUUSD AutoTrader Control Panel*\n\n"
            "Use /help to see available commands.",
            parse_mode="Markdown",
        )

    def _cmd_help(self, chat_id: str) -> None:
        self._send(chat_id,
            "📖 *Available commands:*\n"
            "/status — account summary & trader state\n"
            "/positions — all open positions\n"
            "/pause — enable dry-run (stop sending orders)\n"
            "/resume — disable dry-run (orders go live again)\n"
            "/closeall — ⚠️ emergency close ALL open positions\n"
            "/help — this message",
            parse_mode="Markdown",
        )

    def _cmd_status(self, chat_id: str) -> None:
        try:
            import MetaTrader5 as mt5
            info = mt5.account_info()
            positions = mt5.positions_get(magic=self.trader.magic_number) or []
            paused = "⏸ DRY-RUN (paused)" if self.trader.dry_run else "▶️ LIVE"
            if info:
                msg = (
                    f"📊 *Trader Status*\n"
                    f"Mode: {paused}\n"
                    f"Balance: `${info.balance:,.2f}`\n"
                    f"Equity:  `${info.equity:,.2f}`\n"
                    f"Profit:  `${info.profit:+,.2f}`\n"
                    f"Open positions: `{len(positions)}`\n"
                    f"Assets enabled: `{len(self.trader.pipelines)}`"
                )
            else:
                msg = f"Mode: {paused}\nMT5 not connected."
        except Exception as exc:
            msg = f"❌ Status error: {exc}"
        self._send(chat_id, msg, parse_mode="Markdown")

    def _cmd_positions(self, chat_id: str) -> None:
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(magic=self.trader.magic_number) or []
            if not positions:
                self._send(chat_id, "🟢 No open positions.")
                return
            lines = ["📂 *Open Positions:*"]
            for p in positions:
                direction = "🟢 LONG" if p.type == 0 else "🔴 SHORT"
                lines.append(
                    f"{direction} `{p.symbol}` — {p.volume} lots\n"
                    f"  Entry: `{p.price_open:.5f}` | Now: `{p.price_current:.5f}`\n"
                    f"  PnL: `${p.profit:+.2f}` | SL: `{p.sl:.5f}` TP: `{p.tp:.5f}`"
                )
            self._send(chat_id, "\n".join(lines), parse_mode="Markdown")
        except Exception as exc:
            self._send(chat_id, f"❌ Positions error: {exc}")

    def _cmd_pause(self, chat_id: str) -> None:
        self.trader.dry_run = True
        logger.info("Trader PAUSED via Telegram (dry_run=True)")
        self._send(chat_id, "⏸ Trader *paused*. No new orders will be sent. Use /resume to re-enable.", parse_mode="Markdown")

    def _cmd_resume(self, chat_id: str) -> None:
        self.trader.dry_run = False
        logger.info("Trader RESUMED via Telegram (dry_run=False)")
        self._send(chat_id, "▶️ Trader *resumed*. Live orders are active.", parse_mode="Markdown")

    def _cmd_closeall(self, chat_id: str) -> None:
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(magic=self.trader.magic_number) or []
            if not positions:
                self._send(chat_id, "🟢 No open positions to close.")
                return
            closed, failed = 0, 0
            for pos in positions:
                tick = mt5.symbol_info_tick(pos.symbol)
                if not tick:
                    failed += 1
                    continue
                close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
                price = tick.bid if pos.type == 0 else tick.ask
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": pos.symbol,
                    "volume": pos.volume,
                    "type": close_type,
                    "position": pos.ticket,
                    "price": price,
                    "deviation": 30,
                    "magic": self.trader.magic_number,
                    "comment": "closeall via Telegram",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res.retcode == mt5.TRADE_RETCODE_DONE:
                    closed += 1
                else:
                    failed += 1
                    logger.error("closeall failed for #%d: %s", pos.ticket, res.comment)
            self._send(
                chat_id,
                f"⚠️ *Close All* executed.\nClosed: `{closed}` | Failed: `{failed}`",
                parse_mode="Markdown",
            )
        except Exception as exc:
            self._send(chat_id, f"❌ Close all error: {exc}")

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------
    def _send(self, chat_id: str, text: str, parse_mode: str = "") -> None:
        params: dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            requests.post(f"{self._base}/sendMessage", data=params, timeout=10)
        except Exception as exc:
            logger.warning("Send failed: %s", exc)
