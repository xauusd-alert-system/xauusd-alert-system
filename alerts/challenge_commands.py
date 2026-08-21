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
STATS_FILE = os.path.join(ROOT, "data", "manual", "setup_stats.json")
OUTCOMES_CSV = os.path.join(ROOT, "data", "manual", "setup_outcomes.csv")


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


def cmd_stats(send, chat_id, args=()):
    """/stats — накопительная статистика исходов сетапов A/B (авто-журнал)."""
    if os.path.exists(STATS_FILE):
        try:
            stats = json.load(open(STATS_FILE, encoding="utf-8"))
        except Exception:
            stats = None
        if stats:
            try:
                from challenge.manual import outcomes as outcomes_mod
                send(chat_id, outcomes_mod.format_stats_summary(stats))
            except Exception as exc:
                send(chat_id, f"❌ Stats unavailable: {exc}")
            return
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from challenge.manual import outcomes as outcomes_mod
    except Exception as exc:
        send(chat_id, f"❌ Stats unavailable: {exc}")
        return
    if not os.path.exists(OUTCOMES_CSV):
        send(chat_id, "❓ Журнал исходов пуст — сетапов с алертами ещё не было.")
        return
    rows = outcomes_mod.read_journal(OUTCOMES_CSV)
    stats = outcomes_mod.compute_stats(rows)
    outcomes_mod.save_stats(STATS_FILE, stats)
    send(chat_id, outcomes_mod.format_stats_summary(stats))


def cmd_pairs(send, chat_id, args=()):
    "/pairs [TF] — z-scores и сигналы по всем парам (D1 по умолчанию)."""
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from pairs_analysis import load_config, PairAnalyzer, SignalEngine, EnsembleEngine
    except Exception as exc:
        send(chat_id, f"❌ Pairs module unavailable: {exc}")
        return
    try:
        cfg = load_config()
        analysis = cfg.get("analysis", {})
        thresholds = cfg.get("thresholds", {})
        bt_cfg = dict(analysis)
        bt_cfg.update(cfg.get("backtest", {}) or {})
        tf = args[0].upper() if args else analysis.get("default_timeframe", "D1")
        sig_engine = SignalEngine(thresholds, bt_cfg)
        ens_engine = EnsembleEngine(cfg)
    except Exception as exc:
        send(chat_id, f"❌ Config error: {exc}")
        return
    send(chat_id, f"⏳ Анализирую {len(cfg.get('pairs', []))} пар на {tf}…")
    lines = [f"📊 *PAIRS — {tf}*\n"]
    for pair in cfg.get("pairs", []):
        name = pair["name"]
        try:
            pa = PairAnalyzer(pair, analysis)
            m = pa.analyze(tf)
            sig = sig_engine.current(m)
            ens = ens_engine.forecast(m)
            z = sig.z
            adf_icon = "✅" if m.adf_p < 0.05 else "❌"
            hurst_icon = "✅" if m.hurst < 0.5 else "⚠️"
            sig_icon = {"long": "🟢 LONG", "short": "🔴 SHORT", "none": "⚪ NO EDGE"}
            ens_arrow = {"long": "↑", "short": "↓", "neutral": "→"}
            hl = f"{m.half_life_days:.1f}д" if m.half_life_days < 100 else "∞"
            lines.append(
                f"*{name}* [{m.n_bars} баров]\n"
                f"  z: {z:+.3f}σ | β: {m.beta:.2f} | ratio: {m.ratio:.1f}\n"
                f"  {adf_icon} ADF p={m.adf_p:.4f} | {hurst_icon} H={m.hurst:.2f} | HL={hl}\n"
                f"  Signal: {sig_icon.get(sig.direction, sig.direction)}"
            )
            if sig.valid:
                lines.append(f"    {sig.reason}")
            lines.append(
                f"  Ensemble: {ens_arrow.get(ens.direction, '?')} {ens.direction.upper()} "
                f"CONF {ens.confidence:.0f}%"
            )
            lines.append("")
        except Exception as exc:
            lines.append(f"*{name}* — ❌ {exc}\n")
    # Cumulative pair stats
    try:
        from pairs_analysis.integrations import pair_cumulative_stats
        stats = pair_cumulative_stats()
        if stats["total_trades"] > 0:
            lines.append(
                f"📈 Pair stats: {stats['total_trades']} сделок, "
                f"WR {stats['win_rate_pct']}%, avgR {stats['avg_r']:+.2f}, "
                f"sumR {stats['sum_r']:+.2f}"
            )
    except Exception:
        pass
    send(chat_id, "\n".join(lines), parse_mode="Markdown")


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