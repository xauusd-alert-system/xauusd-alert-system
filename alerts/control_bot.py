"""
Telegram Control Bot — interactive panel to manage the running trader.

Runs INSIDE the trader process in a background thread via long-polling, using
the SAME TELEGRAM_BOT_TOKEN as the alert sender (TelegramAlertBot) — one token,
one process, one getUpdates consumer, so no Telegram polling conflict. Do NOT
run a second polling bot with the same token in parallel
(e.g. scripts/telegram_admin.py) — Telegram rejects concurrent getUpdates
("Conflict: terminated by other getUpdates request").

Authorization: every command that touches account/position data
(/status, /positions, /metrics, /why, /account) and every mutating command
(/pause, /resume, /closeall) is restricted to the admin chat
(TELEGRAM_ADMIN_CHAT_ID, falling back to TELEGRAM_CHAT_ID — the same chat the
alerts are sent to). Fail-closed: without a configured admin id nothing
sensitive is served.

The status commands (/status, /why, /metrics <period>, /account) are strictly
READ-ONLY: they go through alerts/status_commands.py, which only calls
positions_get / account_info / history_deals_get / terminal_info / initialize.
No command in the status path can open, close or modify an order.

Commands
--------
/start   — welcome message
/help    — list all commands
/status  — trader mode, account summary, open positions with floating P&L in $ and R
/positions — list every open position with entry, current price, PnL
/why <ASSET> — why the position in <ASSET> was opened (verbatim entry context)
/metrics — 📊 institutional microstructure metrics (SMC / Smart Money)
/metrics today|week — closed-trade stats (count, WR, PF, expectancy, P&L)
/account — balance, equity, margin, margin level, floating & today's realized P&L
/paper — frozen paper accumulator liveness/sample counter (never outcome metrics)
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
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]   # strip /cmd@botname
        args = tuple(parts[1:])
        logger.info("Command '%s' (args=%s) from chat_id=%s", cmd, args, chat_id)
        try:
            self._dispatch(cmd, chat_id, args)
        except Exception:
            # One bad command must never kill the polling loop (and with it the
            # trader process's only command channel).
            logger.exception("Dispatch failed for command %r", cmd)
            self._send(chat_id, "❌ Internal error while handling the command. See logs.")

    # ------------------------------------------------------------------
    # Command dispatcher
    # ------------------------------------------------------------------
    # Commands exposing account/position data — admin-only (same chat the
    # alerts go to). Mutating commands were already admin-only; this extends
    # the same fail-closed guard to read-outs of sensitive data.
    ADMIN_COMMANDS = frozenset({
        "/status", "/positions", "/metrics", "/why", "/account", "/paper",
        "/pause", "/resume", "/closeall",
    })

    def _dispatch(self, cmd: str, chat_id: str, args: tuple = ()) -> None:
        handlers = {
            "/start":     self._cmd_start,
            "/help":      self._cmd_help,
            "/status":    self._cmd_status,
            "/metrics":   self._cmd_metrics,
            "/positions": self._cmd_positions,
            "/why":       self._cmd_why,
            "/account":   self._cmd_account,
            "/paper":     self._cmd_paper,
            "/pause":     self._cmd_pause,
            "/resume":    self._cmd_resume,
            "/closeall":  self._cmd_closeall,
        }
        fn = handlers.get(cmd)
        if fn is None:
            self._send(chat_id, "❓ Unknown command. Type /help for the list.")
            return
        # Authorization is checked at the START of every sensitive handler —
        # before any MT5 or file access happens (fail-closed).
        if cmd in self.ADMIN_COMMANDS and not self._is_admin(chat_id):
            self._send(chat_id, "⛔ Unauthorised. This command is restricted to the bot owner.")
            return
        fn(chat_id, args)

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
    def _require_admin(self, chat_id: str) -> bool:
        """Authorization guard invoked at the START of every sensitive handler
        (defense in depth on top of the dispatcher check): refuse commands from
        any chat other than the alert recipient/owner, before any MT5 or file
        access. Fail-closed when no admin id is configured."""
        if not self._is_admin(chat_id):
            self._send(chat_id, "⛔ Unauthorised. This command is restricted to the bot owner.")
            return False
        return True

    def _cmd_start(self, chat_id: str, args: tuple = ()) -> None:
        self._send(chat_id,
            "🤖 *XAUUSD AutoTrader Control Panel*\n\n"
            "Use /help to see available commands.",
            parse_mode="Markdown",
        )

    def _cmd_help(self, chat_id: str, args: tuple = ()) -> None:
        self._send(chat_id,
            "📖 *Available commands:*\n"
            "/status — trader state + open positions (P&L в $ и в R)\n"
            "/positions — all open positions\n"
            "/why <ASSET> — почему открыта позиция (контекст входа из сигнала)\n"
            "/metrics — 📊 микроструктурные метрики по РЕАЛЬНОМУ рынку (SMC / Smart Money)\n"
            "/metrics today|week|2week|month|3month|all — подробная статистика закрытых сделок\n"
            "/account — баланс, equity, маржа, плавающий и дневной реализованный P&L\n"
            "/paper — прогресс frozen paper: только счётчики/liveness, без P&L\n"
            "/pause — enable dry-run (stop sending orders)\n"
            "/resume — disable dry-run (orders go live again)\n"
            "/closeall — ⚠️ emergency close ALL open positions\n"
            "/help — this message\n\n"
            "🔒 Команды с данными счёта (/status, /positions, /metrics, /why, /account) "
            "и мутирующие команды доступны только владельцу бота и работают в режиме "
            "«только чтение» (status-команды не могут открывать/закрывать ордера).",
            parse_mode="Markdown",
        )

    def _cmd_metrics(self, chat_id: str, args: tuple = ()) -> None:
        # /metrics <period> -> closed-trade statistics (read-only MT5 history).
        # Bare /metrics -> institutional SMC metrics computed on REAL candles when
        # a live pipeline can provide them, else the pre-existing synthetic demo
        # with an explicit NOT-REAL marker (owner request 2026-08-11: tie metrics
        # to the real market instead of always using the simulator).
        if args:
            period = str(args[0]).lower()
            if period in ("today", "week", "2week", "month", "3month", "all"):
                self._cmd_metrics_period(chat_id, period)
            else:
                self._send(chat_id, "❓ Использование: /metrics [today|week|2week|month|3month|all]")
            return
        try:
            from features.smart_money_metrics import compute_institutional_metrics, format_institutional_metrics_report
            candles = None
            source = "synthetic"
            # Prefer REAL candles from the running trader's pipeline (any enabled
            # asset) so the SMC metrics reflect the current market, not the shim.
            pipeline_cfg = getattr(self.trader, "cfg", None) or {}
            for asset_key, a_cfg in (pipeline_cfg.get("assets") or {}).items():
                if not a_cfg.get("enabled", False):
                    continue
                try:
                    pip = getattr(self.trader, "pipelines", {}).get(asset_key)
                    if pip is not None and hasattr(pip, "get_frame"):
                        frame = pip.get_frame(n=100)
                        if frame is not None and len(frame) >= 10:
                            candles = frame
                            source = f"real:{asset_key}"
                            break
                except Exception:
                    continue
            if candles is None or len(candles) < 10:
                # Fallback: synthetic demo (explicitly labelled).
                from simulation.provider import SimulationProvider
                try:
                    candles = SimulationProvider.get().get_candles("M5", n=100)
                except Exception:
                    candles = None
                if candles is None or len(candles) < 10:
                    metrics = {
                        "manipulation_index": {"display": "n/a", "text": "нет данных."},
                        "zone_strength": {"display": "n/a", "text": "нет данных."},
                        "smf_ratio": {"display": "n/a", "text": "нет данных."},
                        "liquidity_grab": {"display": "n/a", "text": "нет данных."},
                        "delta_confidence": {"display": "n/a", "text": "нет данных."},
                    }
                    msg = "📊 *Метрики по софту на текущий момент*\n\n_Нет данных ни по реальному рынку, ни по симулятору._\n" + format_institutional_metrics_report(metrics)
                    self._send(chat_id, msg, parse_mode="Markdown")
                    return
                source = "synthetic"
            metrics = compute_institutional_metrics(candles)
            label = "РЕАЛЬНЫЙ РЫНОК" if source != "synthetic" else "СИМУЛЯТОР (НЕ реальные данные)"
            msg = format_institutional_metrics_report(metrics)
            msg += f"\n\n_Источник свечей: {label} ({source})_"
            self._send(chat_id, msg, parse_mode="Markdown")
        except Exception as exc:
            self._send(chat_id, f"❌ Metrics error: {exc}")

    def _cmd_status(self, chat_id: str, args: tuple = ()) -> None:
        """Trader mode + account summary + every open position with floating
        P&L in account currency AND in R (vs the initial stop recorded at
        entry). Read-only: no MT5 state is mutated."""
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import status_commands as sc
            if not sc.ensure_mt5_connection():
                self._send(chat_id, "⚠️ MT5 терминал недоступен — статус получить нельзя.")
                return
            m = sc.get_mt5()
            info = m.account_info()
            # ALL open positions across assets on the account (per spec), not
            # only this bot's magic — manual/foreign positions are shown too,
            # mapped to an internal asset key when the symbol is configured.
            positions = list(m.positions_get() or [])
            contexts = sc.load_position_contexts()
            cfg = getattr(self.trader, "cfg", {})
            msg = sc.format_status_report(
                info, positions, contexts, cfg,
                dry_run=bool(getattr(self.trader, "dry_run", False)),
                n_assets=len(getattr(self.trader, "pipelines", {}) or {}),
            )
            # Plain text on purpose: the message embeds model-generated
            # reasoning/regime strings that could break Markdown parsing.
            self._send(chat_id, msg)
        except Exception as exc:
            logger.exception("/status failed")
            self._send(chat_id, f"❌ Status error: {exc}")

    def _cmd_why(self, chat_id: str, args: tuple = ()) -> None:
        """/why <ASSET> — explain why the open position in <ASSET> was entered,
        verbatim from the entry context journal written at order time.
        Read-only; never fabricates a reason when no context was recorded."""
        if not self._require_admin(chat_id):
            return
        if not args:
            known = ", ".join(sorted((getattr(self.trader, "cfg", {}) or {}).get("assets", {}).keys()))
            self._send(chat_id, f"❓ Использование: /why <ASSET>  (например: /why XAUUSD)\nИзвестные активы: {known or 'n/a'}")
            return
        asset_key = str(args[0]).upper()
        try:
            from alerts import status_commands as sc
            cfg = getattr(self.trader, "cfg", {}) or {}
            asset_cfg = cfg.get("assets", {}).get(asset_key)
            if not asset_cfg:
                known = ", ".join(sorted(cfg.get("assets", {}).keys()))
                self._send(chat_id, f"❓ Неизвестный актив «{asset_key}». Известные: {known or 'n/a'}")
                return
            mt5_symbol = asset_cfg.get("mt5_symbol", asset_key)
            if not sc.ensure_mt5_connection():
                self._send(chat_id, "⚠️ MT5 терминал недоступен — контекст получить нельзя.")
                return
            m = sc.get_mt5()
            positions = list(m.positions_get(symbol=mt5_symbol) or [])
            position = positions[0] if positions else None
            if len(positions) > 1:
                logger.warning("/why %s: %d open positions; explaining #%s",
                               asset_key, len(positions), getattr(position, "ticket", "?"))
            contexts = sc.load_position_contexts()
            context = contexts.get(str(getattr(position, "ticket", ""))) if position else None
            msg = sc.format_why_report(asset_key, mt5_symbol, position, context)
            if len(positions) > 1:
                msg += f"\n\n(Открыто позиций по {asset_key}: {len(positions)}; показан контекст первой — #{getattr(position, 'ticket', '?')})"
            self._send(chat_id, msg)  # plain text: embeds model reasoning
        except Exception as exc:
            logger.exception("/why failed")
            self._send(chat_id, f"❌ Why error: {exc}")

    def _cmd_metrics_period(self, chat_id: str, period: str) -> None:
        """/metrics today|week — closed-trade statistics from deal history.
        Read-only (history_deals_get only)."""
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import status_commands as sc
            if not sc.ensure_mt5_connection():
                self._send(chat_id, "⚠️ MT5 терминал недоступен — метрики получить нельзя.")
                return
            dt_from, dt_to, label = sc.period_range(period)
            deals = sc.fetch_deals_between(dt_from, dt_to)
            contexts = sc.load_position_contexts()
            cfg = getattr(self.trader, "cfg", {})
            msg = sc.format_metrics_report(deals, contexts, cfg, label)
            self._send(chat_id, msg)
        except Exception as exc:
            logger.exception("/metrics %s failed", period)
            self._send(chat_id, f"❌ Metrics error: {exc}")

    def _cmd_paper(self, chat_id: str, args: tuple = ()) -> None:
        """Frozen-paper liveness only; never reads close-event outcome payloads."""
        if not self._require_admin(chat_id):
            return
        try:
            import os
            from data.paper_ledger import paper_accumulation_status
            from paper.accumulator import format_accumulation_status, load_frozen_manifest

            manifest_path = os.getenv("PAPER_MANIFEST_PATH")
            db_path = os.getenv("PAPER_LEDGER_DB", "data/paper_forward.sqlite")
            if not manifest_path:
                self._send(chat_id, "ℹ️ PAPER_MANIFEST_PATH не настроен; frozen paper accumulator не запущен.")
                return
            manifest = load_frozen_manifest(manifest_path, verify_model=False)
            status = paper_accumulation_status(db_path, manifest["run_id"])
            self._send(chat_id, format_accumulation_status(status))
        except Exception as exc:
            logger.exception("/paper failed")
            self._send(chat_id, f"❌ Paper status error: {exc}")

    def _cmd_account(self, chat_id: str, args: tuple = ()) -> None:
        """/account — balance, equity, margin, margin level, floating P&L and
        today's realized P&L. Read-only (account_info + history_deals_get)."""
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import status_commands as sc
            if not sc.ensure_mt5_connection():
                self._send(chat_id, "⚠️ MT5 терминал недоступен — данные счёта получить нельзя.")
                return
            m = sc.get_mt5()
            info = m.account_info()
            realized = sc.realized_pnl_today()
            msg = sc.format_account_report(info, realized)
            self._send(chat_id, msg)
        except Exception as exc:
            logger.exception("/account failed")
            self._send(chat_id, f"❌ Account error: {exc}")

    def _cmd_positions(self, chat_id: str, args: tuple = ()) -> None:
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

    def _cmd_pause(self, chat_id: str, args: tuple = ()) -> None:
        self.trader.dry_run = True
        logger.info("Trader PAUSED via Telegram (dry_run=True)")
        self._send(chat_id, "⏸ Trader *paused*. No new orders will be sent. Use /resume to re-enable.", parse_mode="Markdown")

    def _cmd_resume(self, chat_id: str, args: tuple = ()) -> None:
        self.trader.dry_run = False
        logger.info("Trader RESUMED via Telegram (dry_run=False)")
        self._send(chat_id, "▶️ Trader *resumed*. Live orders are active.", parse_mode="Markdown")

    def _cmd_closeall(self, chat_id: str, args: tuple = ()) -> None:
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
