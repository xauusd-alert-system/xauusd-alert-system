"""US Stocks Telegram commands — extension of the existing bot (ТЗ §6.3/§9).

One token, one getUpdates consumer. Under profile us_stocks_challenge the
legacy control bot does not run, so `usstocks.bot` owns polling and
dispatches through this controller; the raw-API patterns (admin fail-closed,
stale-update guard) mirror alerts/control_bot.py.

HARD RULE (ТЗ §9/§12.16): every mutation of P&L or risk-state is applied
ONLY after the user presses ✅ on an inline keyboard. ❌ or expiry discards.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

logger = logging.getLogger("usstocks.commands")

CONFIRM_TTL_SECONDS = 300            # unconfirmed actions expire


@dataclass
class PendingAction:
    kind: str                       # pnl | stop | resume | flat
    payload: dict = field(default_factory=dict)
    chat_id: str = ""
    created_ts: float = 0.0

    def describe(self) -> str:
        if self.kind == "pnl":
            amt = self.payload["amount"]
            return f"PnL {amt:+.2f}$ ({self.payload['label']})"
        if self.kind == "stop":
            return "СТОП-ДЕНЬ (новые сигналы заблокированы)"
        if self.kind == "resume":
            return "снять стоп-день"
        if self.kind == "flat":
            sym = self.payload.get("symbol") or "-"
            return f"закрыть/обнулить активную позицию ({sym})"
        return self.kind


class UsCommandsController:
    """Framework-free handlers; `transport` abstracts sendMessage/callback."""

    def __init__(self, *, journal, state: "RiskState", admin_id: str,
                 transport, clock: Callable[[], float] = time.time):
        self.journal = journal                 # usstocks.journal.UsJournal
        self.state = state                     # shared RiskState (live object)
        self.admin_id = str(admin_id or "")
        self.transport = transport             # .send(chat,text,markup=None)
        self.clock = clock
        self.signals_enabled: bool = True
        self._pending: Dict[str, PendingAction] = {}   # chat_id -> action

    # ------------------------------------------------------------------
    # Authorization (fail-closed, same convention as alerts/control_bot.py)
    # ------------------------------------------------------------------

    def _is_admin(self, chat_id: str) -> bool:
        return bool(self.admin_id) and chat_id == self.admin_id

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def handle_command(self, cmd: str, chat_id: str, args: tuple) -> None:
        if not cmd.startswith("/us"):
            return                              # not ours — ignore silently
        if not self._is_admin(chat_id):
            self.transport.send(chat_id, "🔒 /us_* — только для владельца бота.")
            return
        handlers = {
            "/us_status":   lambda cid, a: self._cmd_status(cid),
            "/us_watchlist": lambda cid, a: self._cmd_watchlist(cid),
            "/us_signals":  lambda cid, a: self._cmd_signals(cid, a),
            "/us_pnl":      lambda cid, a: self._request_pnl(cid, a),
            "/us_win":      lambda cid, a: self._request_pnl(cid, a, positive=True),
            "/us_loss":     lambda cid, a: self._request_pnl(cid, a, negative=True),
            "/us_flat":     lambda cid, a: self._request(cid, PendingAction(
                kind="flat", payload={"symbol": self.state.active_symbol},
                chat_id=cid)),
            "/us_stop":     lambda cid, a: self._request(cid, PendingAction(
                kind="stop", chat_id=cid)),
            "/us_resume":   lambda cid, a: self._request(cid, PendingAction(
                kind="resume", chat_id=cid)),
            "/us_export":   lambda cid, a: self._cmd_export(cid, a),
        }
        fn = handlers.get(cmd)
        if fn is None:
            known = ", ".join(sorted(handlers))
            self.transport.send(chat_id, f"Неизвестно. Команды: {known}")
            return
        try:
            fn(chat_id, args)
        except Exception as e:                  # never kill the poll loop
            logger.exception("us command failed")
            self.transport.send(chat_id, f"❌ Ошибка обработки: {e}")

    def handle_callback(self, data: str, chat_id: str,
                        callback_id: Optional[str] = None) -> None:
        try:
            if callback_id:
                self.transport.answer_callback(callback_id)
            if data == "us:cancel":
                self._pending.pop(chat_id, None)
                self.transport.send(chat_id, "↩️ Отменено.")
                return
            if data != "us:confirm":
                return
            act = self._pending.pop(chat_id, None)
            if act is None:
                self.transport.send(chat_id, "Нет действия на подтверждении.")
                return
            if self.clock() - act.created_ts > CONFIRM_TTL_SECONDS:
                self.transport.send(chat_id, "⌛ Подтверждение истекло.")
                return
            self._apply(act, chat_id)
        except Exception as e:
            logger.exception("callback failed")
            self.transport.send(chat_id, f"❌ Ошибка подтверждения: {e}")

    # ------------------------------------------------------------------
    # Mutation requests (preview + keyboard)
    # ------------------------------------------------------------------

    def _keyboard(self) -> dict:
        return {"inline_keyboard": [[
            {"text": "✅ Принял", "callback_data": "us:confirm"},
            {"text": "❌ Отклонил", "callback_data": "us:cancel"},
        ]]}

    def _request(self, chat_id: str, act: PendingAction) -> None:
        act.created_ts = self.clock()
        self._pending[chat_id] = act
        self.transport.send(
            chat_id,
            f"Подтвердите действие:\n<b>{act.describe()}</b>\n"
            f"Изменение применится только после «✅ Принял».",
            reply_markup=self._keyboard())

    def _request_pnl(self, chat_id: str, args: tuple,
                     positive: bool = False, negative: bool = False) -> None:
        usage = "Использование: /us_pnl <amount>  (или /us_win, /us_loss <amount>)"
        if not args:
            self.transport.send(chat_id, usage)
            return
        try:
            amount = abs(float(args[0].replace(",", ".")))
        except ValueError:
            self.transport.send(chat_id, usage)
            return
        label = ("win" if positive else "loss" if negative else "manual")
        if negative:
            amount = -amount
        self._request(chat_id, PendingAction(
            kind="pnl", payload={"amount": amount, "label": label},
            chat_id=chat_id))

    # ------------------------------------------------------------------
    # Apply confirmed actions — THE ONLY mutation path
    # ------------------------------------------------------------------

    def _apply(self, act: PendingAction, chat_id: str) -> None:
        session_date = getattr(self.state, "session_date", "") or "unknown"
        if act.kind == "pnl":
            amount = float(act.payload["amount"])
            self.state.realized_pnl_usd += amount
            self.state.trades_taken += 1
            if amount > 0:
                self.state.consecutive_losses = 0
            elif amount < 0:
                self.state.consecutive_losses += 1
            row = self.journal.latest_signal(
                symbol=self.state.active_symbol or None)
            if row is not None:
                self.journal.record_outcome(
                    row["signal_id"], pnl_usd=amount,
                    planned_risk_usd=row["planned_risk_usd"],
                    confirmed_by=chat_id, note=act.payload["label"])
                self.state.active_symbol = None
            self.transport.send(
                chat_id,
                f"✅ Записано {amount:+.2f}$. День: "
                f"{self.state.realized_pnl_usd:+.2f}$, "
                f"сделок {self.state.trades_taken}, "
                f"лоссов подряд {self.state.consecutive_losses}.")
        elif act.kind == "flat":
            self.state.active_symbol = None
            self.transport.send(chat_id, "✅ Активная позиция обнулена.")
        elif act.kind == "stop":
            self.state.day_stopped = True
            self.transport.send(chat_id, "⛔ Стоп-день установлен.")
        elif act.kind == "resume":
            self.state.day_stopped = False
            self.transport.send(chat_id, "▶️ Торговый день возобновлён.")
        else:
            self.transport.send(chat_id, "Неизвестное действие.")

    # ------------------------------------------------------------------
    # Read-only commands
    # ------------------------------------------------------------------

    def _cmd_status(self, chat_id: str) -> None:
        s = self.state
        day = self.journal.day_pnl(s.session_date) if s.session_date else 0.0
        self.transport.send(
            chat_id,
            f"/us_status\nДата: {s.session_date or '—'}\n"
            f"Realized: {s.realized_pnl_usd:+.2f}$ | журнал: {day:+.2f}$\n"
            f"Unrealized: {s.unrealized_pnl_usd:+.2f}$\n"
            f"Сделок: {s.trades_taken} | лоссов подряд: "
            f"{s.consecutive_losses}\nАктивная позиция: "
            f"{s.active_symbol or 'нет'}\nСтоп-день: "
            f"{'ДА' if s.day_stopped else 'нет'} | сигналы: "
            f"{'вкл' if self.signals_enabled else 'выкл'}")

    def _cmd_watchlist(self, chat_id: str) -> None:
        from usstocks.premarket_ranker import format_watchlist_message
        items = getattr(self.journal, "last_watchlist_items", None)
        if callable(items):
            items = items()
        self.transport.send(chat_id, format_watchlist_message(items or []))

    def _cmd_signals(self, chat_id: str, args: tuple = ()) -> None:
        arg = (args[0].lower() if args else "")
        if arg in ("on", "вкл"):
            self.signals_enabled = True
            self.transport.send(chat_id, "✅ Сигналы включены.")
        elif arg in ("off", "выкл"):
            self.signals_enabled = False
            self.transport.send(chat_id, "🔇 Сигналы выключены.")
        else:
            self.transport.send(chat_id,
                                f"Сигналы сейчас: "
                                f"{'вкл' if self.signals_enabled else 'выкл'}."
                                f" Использование: /us_signals on|off")

    def _cmd_export(self, chat_id: str, args: tuple) -> None:
        date = args[0] if args else (self.state.session_date or "")
        if not date:
            self.transport.send(chat_id, "Использование: /us_export YYYY-MM-DD")
            return
        path = self.journal.export_day_csv(date, "data/usstocks_export")
        doc = getattr(self.transport, "send_document", None)
        if doc:
            doc(chat_id, path)
        else:
            self.transport.send(chat_id, f"📄 Экспорт: {path}")
