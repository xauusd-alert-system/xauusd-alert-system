# -*- coding: utf-8 -*-
"""Challenge (HashHedge manual system) commands for the shared Telegram control bot.

One bot, two systems, separated files:
  forex/system commands  -> alerts/control_bot.py + alerts/status_commands.py
  challenge commands     -> this module (alerts/challenge_commands.py)

control_bot imports this module lazily at command time, so the bot keeps
serving forex commands even if the challenge package is temporarily absent
from the working tree (it is committed to both branches; see README).

Commands added here:
  /day        — состояние дня ручной системы (профиль, лимиты, PnL, статус)
  /journal    — последние сделки + сводка по дням (журнал ТЗ §7)
  /scan       — разовый live-скан watchlist (сетапы A/B)
  /alert      — статус алертера (что отправлено сегодня)
"""
import datetime as dt
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANUAL_DIR = os.path.join(ROOT, "challenge", "manual")
STATE_FILE = os.path.join(ROOT, "data", "manual", "day_state.json")
SENT_FILE = os.path.join(ROOT, "data", "manual", "alerts_sent.json")
JOURNAL_FILE = os.path.join(ROOT, "data", "manual", "journal.csv")


def _import_manual():
    """Lazy import of the challenge package. Returns (module, err)."""
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from challenge.manual import risk, journal  # noqa: F401
        return (risk, journal), None
    except Exception as exc:
        return None, str(exc)


def _require(send, chat_id) -> tuple:
    mods, err = _import_manual()
    if err:
        send(chat_id, f"❌ Challenge system unavailable: {err}")
        return None
    return mods


def cmd_day(send, chat_id, args=()):
    """/day — текущее состояние дня (машина состояний ТЗ §6)."""
    mods = _require(send, chat_id)
    if not mods:
        return
    risk, _ = mods
    sm = risk.DailyStateMachine()
    s = sm.state
    if not os.path.exists(STATE_FILE):
        send(chat_id, "❓ День ещё не начат: `day start --stage 1 --profile B --equity 1000`")
        return
    lines = [
        f"День {s.stage}-й (профиль {s.profile}), дата {s.date}",
        f"Equity {s.current_equity:.2f} | старт дня {s.day_start_equity:.2f}",
        f"PnL дня {s.daily_pnl():+.2f}$ ({100*s.daily_pnl()/s.day_start_equity:+.2f}%)",
        f"Сделок {s.trades_today}/{s.effective_max_trades} | убытков {s.losses_today}",
        f"Риск/сделку {s.effective_risk_usd:.2f}$ | только A-сетапы: {'да' if s.effective_only_a else 'нет'}",
        f"Статус: {s.status} — {s.status_reason}",
    ]
    if s.paused_until:
        lines.append(f"Пауза до {s.paused_until}")
    send(chat_id, "\n".join(lines))


def cmd_journal(send, chat_id, args=()):
    """/journal [N] — последние N сделок + сводка по дням (ТЗ §7)."""
    mods = _require(send, chat_id)
    if not mods:
        return
    _, journal = mods
    if not os.path.exists(JOURNAL_FILE):
        send(chat_id, "❓ Журнал пуст — сделок ещё не было.")
        return
    try:
        n = int(args[0]) if args and str(args[0]).isdigit() else 5
    except (ValueError, IndexError):
        n = 5
    n = max(1, min(n, 20))
    rows = journal.read(JOURNAL_FILE)
    if not rows:
        send(chat_id, "❓ Журнал пуст.")
        return
    last = rows[-n:]
    lines = ["📓 *Последние сделки:*"]
    for r in last:
        res = r.get("result_usd") or "открыта"
        lines.append(
            f"#{r['num']} {r['date']} {r['time']} {r['instrument']} "
            f"{'L' if r['direction'].upper() == 'L' else 'S'} "
            f"[{r['setup_class']}] вход {r['entry_price']} стоп {r['stop']} "
            f"→ {res}$ {r.get('outcome', '')}"
        )
    for d in journal.daily_summary(JOURNAL_FILE):
        lines.append(
            f"{d['date']}: {d['trades']} сделок, PnL {d['pnl_usd']:+.2f}$, "
            f"WR {d['win_rate_pct']}%, avg R {d['avg_r']}"
        )
    send(chat_id, "\n".join(lines), parse_mode="Markdown")


def cmd_scan(send, chat_id, args=()):
    """/scan — разовый live-скан watchlist через UTEX API (ТЗ §4)."""
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from challenge.manual import alerter
    except Exception as exc:
        send(chat_id, f"❌ Scanner unavailable: {exc}")
        return
    send(chat_id, "⏳ Сканирую watchlist (10 инструментов)…")
    try:
        access = alerter.refresh_access()
        hits = alerter.scan_watchlist(access)
    except Exception as exc:
        send(chat_id, f"❌ Scan error: {exc}")
        return
    if not hits:
        send(chat_id, "🔎 Сетапов A/B сейчас нет (тренд/импульс/откат не совпали).")
        return
    for res in hits:
        send(chat_id, alerter.format_setup(res))


def cmd_alert(send, chat_id, args=()):
    """/alert — статус алертера и отправленные сегодня сетапы."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    lines = []
    if os.path.exists(SENT_FILE):
        try:
            sent = json.load(open(SENT_FILE, encoding="utf-8"))
        except Exception:
            sent = {}
        today_sent = {k: v for k, v in sent.items() if k.startswith(today)}
        if today_sent:
            for k, v in today_sent.items():
                lines.append(f"{k}: {v.get('grade')} @ {v.get('entry')} → {v.get('target')}")
        else:
            lines.append("Сегодня алертов ещё не было.")
    else:
        lines.append("Файл алертов не найден — алертер, вероятно, не запущен.")
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from challenge.manual import risk as risk_mod
        sm = risk_mod.DailyStateMachine()
        s = sm.state
        lines.append(f"День: {s.trades_today}/{s.effective_max_trades} сделок, "
                     f"PnL {s.daily_pnl():+.2f}$, статус {s.status}")
    except Exception as exc:
        lines.append(f"Состояние дня недоступно: {exc}")
    send(chat_id, "\n".join(lines))