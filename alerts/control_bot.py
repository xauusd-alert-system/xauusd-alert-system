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

try:
    from alerts.pair_monitor import PairMonitor
except ImportError:
    PairMonitor = None

if TYPE_CHECKING:
    from execution.mt5_trader import MultiAssetMT5Trader

logger = logging.getLogger("control_bot")


def parse_admin_ids(raw: str | None) -> frozenset[str]:
    """ТЗ 10.3: parse ``TELEGRAM_ADMIN_IDS`` (comma-separated user/chat ids).

    Tolerates whitespace and trailing commas; non-numeric entries are dropped
    (they can never match a Telegram chat id, so keeping them would be
    misleading). Empty/None -> empty whitelist (fail-closed unchanged).
    """
    if not raw:
        return frozenset()
    ids: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(part)
    return frozenset(ids)


class TelegramControlBot:
    """Long-polling Telegram bot that controls a running MultiAssetMT5Trader."""

    POLL_TIMEOUT = 30          # seconds for long-poll
    RETRY_SLEEP  = 5           # seconds between retries on network error
    MAX_BACKOFF  = 60          # audit B: cap for exponential poll backoff
    STALE_UPDATE_SECONDS = 600 # audit C: ignore updates older than this

    def __init__(self, trader: "MultiAssetMT5Trader") -> None:
        self.trader = trader
        self.token: str = get_env("TELEGRAM_BOT_TOKEN", required=True)
        # Admin chat ID: only this chat can issue mutating commands.
        self.admin_id: str = str(
            get_env("TELEGRAM_ADMIN_CHAT_ID", required=False)
            or get_env("TELEGRAM_CHAT_ID", required=False)
            or ""
        )
        # ТЗ 10.3: additional admin whitelist (comma-separated ids). The
        # single admin chat above stays authoritative; the whitelist only
        # EXTENDS access, never relaxes the fail-closed no-config refusal.
        self.admin_ids: frozenset[str] = parse_admin_ids(
            get_env("TELEGRAM_ADMIN_IDS", required=False)
        )
        self._base = f"https://api.telegram.org/bot{self.token}"
        self._offset: int = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Pair monitor: background thread for pair-signal alerts (24/7)
        self._pair_monitor: Optional[PairMonitor] = None

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
        # Start pair monitor (24/7 background thread)
        try:
            self._pair_monitor = PairMonitor(
                send_fn=self._send,
                admin_chat_id=self.admin_id,
            )
            self._pair_monitor.start()
        except Exception as e:
            logger.warning("Pair monitor failed to start: %s", e)

    def stop(self) -> None:
        self._stop.set()
        if self._pair_monitor:
            self._pair_monitor.stop()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        # Audit B: exponential backoff — a 409 conflict / network outage used
        # to produce a log line every 5s indefinitely.
        backoff = self.RETRY_SLEEP
        while not self._stop.is_set():
            try:
                updates = self._get_updates()
                backoff = self.RETRY_SLEEP  # success resets the backoff
                for upd in updates:
                    self._handle_update(upd)
            except Exception as exc:
                logger.warning("Poll error: %s (retrying in %ss)", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)

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
        # Audit C: replay guard. The offset lives only in memory, so after a
        # crash+restart every unconfirmed update re-fires — including mutating
        # commands like /closeall. Commands older than 10 minutes are stale.
        # Updates without a timestamp can't be judged -> treat as fresh.
        date_ts = msg.get("date")
        if date_ts is not None:
            try:
                age = time.time() - int(date_ts)
            except (TypeError, ValueError):
                age = 0
            if age > self.STALE_UPDATE_SECONDS:
                logger.info("Skipping stale update (%ss old)", int(age))
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
        # challenge (HashHedge manual system) commands
        "/day", "/journal", "/scan", "/alert", "/stats",
        # forex: pairs analysis (pairs_analysis module)
        "/pairs",
        # news: economic calendar
        "/news",
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
            # challenge (HashHedge manual system) commands — one bot, two systems
            "/day":       self._cmd_challenge_day,
            "/journal":   self._cmd_challenge_journal,
            "/scan":      self._cmd_challenge_scan,
            "/alert":     self._cmd_challenge_alert,
            "/stats":     self._cmd_challenge_stats,
            "/pairs":     self._cmd_challenge_pairs,
            "/news":      self._cmd_news,
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
        if not self.admin_id and not self.admin_ids:
            logger.warning(
                "Telegram admin not configured (TELEGRAM_ADMIN_CHAT_ID/TELEGRAM_CHAT_ID/"
                "TELEGRAM_ADMIN_IDS empty) - refusing mutating command from chat_id=%s",
                chat_id,
            )
            return False
        if chat_id == self.admin_id or chat_id in self.admin_ids:
            return True
        if self.admin_ids:
            logger.warning(
                "Unauthorized Telegram command from chat_id=%s (not in TELEGRAM_ADMIN_IDS "
                "and not the admin chat)", chat_id,
            )
        return False

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
            "🤖 *Control Panel — XAUUSD AutoTrader + HashHedge Challenge*\n\n"
            "Один бот, две системы. Use /help to see available commands.",
            parse_mode="Markdown",
        )

    def _cmd_help(self, chat_id: str, args: tuple = ()) -> None:
        self._send(chat_id,
            "📖 *Available commands:*\n"
            "— XAUUSD (forex-система) —\n"
            "/status — trader state + open positions (P&L в $ и в R)\n"
            "/positions — all open positions\n"
            "/why <ASSET> — почему открыта позиция (контекст входа из сигнала)\n"
            "/metrics — 📊 микроструктурные метрики по РЕАЛЬНОМУ рынку (SMC / Smart Money)\n"
            "/metrics today|week|2week|month|3month|all — подробная статистика закрытых сделок\n"
            "/account — баланс, equity, маржа, плавающий и дневной реализованный P&L\n"
            "/paper — прогресс frozen paper: только счётчики/liveness, без P&L\n"
            "/pause — дополнительный runtime brake (dry-run)\n"
            "/resume — снять runtime brake; deployment.mode и allowlist всё равно обязательны\n"
            "/closeall — ⚠️ emergency close ALL open positions\n"
            "/pairs [TF] — z-scores и сигналы по всем парам (pairs_analysis, 24/7)\n"
            "/news — ближайшие HIGH-impact события + статус news guard\n"
            "— HashHedge Challenge (ручная система) —\n"
            "/day — состояние дня (профиль, лимиты, PnL, статус)\n"
            "/journal — последние сделки + сводка по дням\n"
            "/scan — разовый live-скан watchlist (сетапы A/B)\n"
            "/alert — статус алертера и отправленные сетапы\n"
            "/stats — накопительная статистика исходов сетапов A/B\n"
            "/help — this message\n\n"
            "🔒 Команды с данными счёта (/status, /positions, /metrics, /why, /account) "
            "и мутирующие команды доступны только владельцу бота и работают в режиме "
            "«только чтение» (status-команды не могут открывать/закрывать ордера).",
            parse_mode="Markdown",
        )

    # ------------------------------------------------------------------
    # Challenge (HashHedge manual system) command handlers.
    # Implemented in alerts/challenge_commands.py (separated file) and
    # imported lazily so forex commands keep working regardless of branch.
    # ------------------------------------------------------------------
    def _cmd_challenge_day(self, chat_id: str, args: tuple = ()) -> None:
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import challenge_commands as cc
            cc.cmd_day(self._send, chat_id, args)
        except Exception as exc:
            logger.exception("/day failed")
            self._send(chat_id, f"❌ Day error: {exc}")

    def _cmd_challenge_journal(self, chat_id: str, args: tuple = ()) -> None:
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import challenge_commands as cc
            cc.cmd_journal(self._send, chat_id, args)
        except Exception as exc:
            logger.exception("/journal failed")
            self._send(chat_id, f"❌ Journal error: {exc}")

    def _cmd_challenge_scan(self, chat_id: str, args: tuple = ()) -> None:
        if not self._require_admin(chat_id):
            return
        # Run the scan in a background thread so the polling loop (and with it
        # the trader's only command channel) never blocks on API round-trips.
        def _worker():
            try:
                from alerts import challenge_commands as cc
                cc.cmd_scan(self._send, chat_id, args)
            except Exception as exc:
                logger.exception("/scan failed")
                self._send(chat_id, f"❌ Scan error: {exc}")
        threading.Thread(target=_worker, name="tg-challenge-scan", daemon=True).start()

    def _cmd_challenge_alert(self, chat_id: str, args: tuple = ()) -> None:
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import challenge_commands as cc
            cc.cmd_alert(self._send, chat_id, args)
        except Exception as exc:
            logger.exception("/alert failed")
            self._send(chat_id, f"❌ Alert status error: {exc}")

    def _cmd_challenge_stats(self, chat_id: str, args: tuple = ()) -> None:
        if not self._require_admin(chat_id):
            return
        try:
            from alerts import challenge_commands as cc
            cc.cmd_stats(self._send, chat_id, args)
        except Exception as exc:
            logger.exception("/stats failed")
            self._send(chat_id, f"❌ Stats error: {exc}")

    def _cmd_news(self, chat_id: str, args: tuple = ()) -> None:
        "/news — upcoming high-impact events + news guard status."""
        if not self._require_admin(chat_id):
            return
        try:
            from news.calendar_feed import get_feed
            from news.guard import get_guard

            feed = get_feed()
            guard = get_guard()

            # Determine asset context from args
            asset_key = str(args[0]).upper() if args else None
            if asset_key and asset_key not in ("XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD", "ALL"):
                self._send(chat_id, f"❓ Неизвестный актив: {asset_key}. Доступные: XAUUSD, XAGUSD, BTCUSD, EURUSD, GBPUSD, ALL")
                return

            hours = 48.0
            # /news 24 or /news 72 — custom window
            for a in args:
                try:
                    hours = float(a)
                except ValueError:
                    pass

            # Guard status
            guard_status = guard.status_text(asset_key if asset_key != "ALL" else None)

            # Upcoming events
            currencies = None
            if asset_key and asset_key != "ALL":
                from news.guard import ASSET_CURRENCIES
                currencies = ASSET_CURRENCIES.get(asset_key)

            events_text = feed.format_upcoming(hours=hours)

            msg = f"{guard_status}\n\n{events_text}"
            self._send(chat_id, msg)
        except Exception as exc:
            logger.exception("/news failed")
            self._send(chat_id, f"❌ News error: {exc}")

    def _cmd_challenge_pairs(self, chat_id: str, args: tuple = ()) -> None:
        "/pairs [TF] — z-scores and signals for all monitored pairs."
        if not self._require_admin(chat_id):
            return
        self._send(chat_id, "⏳ Loading pair data...")
        try:
            if self._pair_monitor:
                msg = self._pair_monitor.query_all()
            else:
                from alerts.pair_monitor import PairMonitor
                pm = PairMonitor(send_fn=self._send, admin_chat_id=chat_id)
                msg = pm.query_all()
            self._send(chat_id, msg)
        except Exception as exc:
            logger.exception("/pairs failed")
            self._send(chat_id, f"❌ Pairs error: {exc}")

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
                self._send(
                    chat_id,
                    "📊 Institutional metrics unavailable: no real closed-candle source. "
                    "Synthetic fallback is disabled.",
                )
                return
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
            resp = requests.post(f"{self._base}/sendMessage", data=params, timeout=10)
            # Audit A: check delivery. Telegram answers 400 for broken Markdown
            # (a symbol with _ or * in /positions output) — retry once as plain
            # text so the answer is never silently lost.
            if not resp.ok and parse_mode:
                params.pop("parse_mode")
                resp = requests.post(f"{self._base}/sendMessage", data=params, timeout=10)
            if not resp.ok:
                logger.warning("sendMessage to %s failed: %s %s",
                               chat_id, resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Send failed: %s", exc)
