"""
Read-only MT5 account/position status reports for the Telegram control bot.

This module backs the /status, /why, /metrics <period> and /account commands
registered in alerts/control_bot.py. It reads from the already-running,
logged-in MT5 terminal (real MetaTrader5 package on the Windows VPS, or the
bundled simulation shim under tests / virtual runs) and from the entry-context
journal (logs/live_positions.json) written by execution/mt5_trader.py.

SECURITY (enforced by alerts/tests/test_status_commands.py::test_status_module_is_read_only):
this module is strictly READ-ONLY. Allowed MT5 calls only:
    mt5.initialize(), mt5.terminal_info(), mt5.positions_get(),
    mt5.account_info(), mt5.history_deals_get()
It must NEVER call order_send / order_close / order_check or any other
mutating MT5 function. All MT5 access goes through the lazily-loaded module
handle `_mt5` so the alert bot stays import-safe on machines without the
MetaTrader5 package.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Optional

logger = logging.getLogger("status_commands")

# Entry-context journal written by execution/mt5_trader.record_position_context.
# Keep in sync with execution.mt5_trader.LIVE_POSITIONS_PATH — the trader module
# is deliberately NOT imported here (it would pull the whole ML stack and a hard
# MetaTrader5 import into the alert layer). Same env-var name on both sides keeps
# a single override point.
LIVE_POSITIONS_PATH = os.getenv("LIVE_POSITIONS_PATH", "logs/live_positions.json")

# MT5 deal entry types (mirrors MetaTrader5 constants without importing them at
# module import time): 0 = IN, 1 = OUT, 2 = INOUT, 3 = OUT_BY. Any non-IN deal
# realizes PnL and counts as a "closed trade" for /metrics.
_DEAL_ENTRY_IN = 0

# ---------------------------------------------------------------------------
# Lazy MT5 access (the control bot must stay import-safe without the package)
# ---------------------------------------------------------------------------

_mt5 = None
_mt5_import_failed = False


def _load_mt5():
    """Import MetaTrader5 on first use and cache the module handle.

    Never raises: returns None when the package is not importable (e.g. a
    non-Windows dev machine without the simulation shim on sys.path)."""
    global _mt5, _mt5_import_failed
    if _mt5 is None and not _mt5_import_failed:
        try:
            from mt5_adapter.lazy import get_mt5_module

            _mt5 = get_mt5_module()
        except Exception:  # ImportError and anything a broken install raises
            _mt5_import_failed = True
            logger.warning("MetaTrader5 package is not importable in this process")
    return _mt5


def get_mt5():
    """Public, test-friendly access to the cached MetaTrader5 module handle.

    The command handlers in alerts/control_bot.py route EVERY MT5 call through
    this accessor so that tests can substitute a fake module in one place
    (monkeypatch alerts.status_commands._mt5) and so the read-only surface of
    this module cannot be bypassed accidentally. Returns None when the package
    is unavailable."""
    return _load_mt5()


def ensure_mt5_connection() -> bool:
    """Initialize connection to the already-running, logged-in MT5 terminal.
    Never raises; handlers must degrade gracefully if MT5 is unreachable."""
    m = _load_mt5()
    if m is None:
        return False
    try:
        if m.terminal_info() is not None:
            return True
        return bool(m.initialize())
    except Exception as exc:
        logger.warning("MT5 connection check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Entry-context journal (logs/live_positions.json)
# ---------------------------------------------------------------------------

def load_position_contexts(path: Optional[str] = None) -> dict:
    """Load the open-position entry contexts keyed by ticket (str).

    Late-binds LIVE_POSITIONS_PATH so tests can redirect the module constant.
    Never raises: a missing/corrupt file yields an empty mapping (the /why
    command then reports "context unavailable" instead of crashing)."""
    path = path or LIVE_POSITIONS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read position contexts from %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def symbol_to_asset_map(cfg: dict) -> dict:
    """Reverse mapping MT5 symbol -> internal asset key (e.g. GOLD -> XAUUSD)."""
    out = {}
    for asset_key, a_cfg in (cfg or {}).get("assets", {}).items():
        sym = a_cfg.get("mt5_symbol")
        if sym:
            out[sym] = asset_key
    return out


def point_value_lot_for(cfg: dict, asset_key: Optional[str]) -> float:
    """USD notional per 1.0 lot per 1.0 price unit for the asset.

    Same resolution order as model/ensemble_backtest.py: per-asset override,
    then the backtest section default, then 100.0 (gold: 1 lot = 100 oz)."""
    asset_cfg = (cfg or {}).get("assets", {}).get(asset_key, {}) if asset_key else {}
    return float(asset_cfg.get(
        "point_value_lot",
        (cfg or {}).get("backtest", {}).get("point_value_lot", 100.0),
    ))


def floating_r(profit: float, entry_price: float, initial_stop: Optional[float],
               volume: float, point_value_lot: float) -> Optional[float]:
    """Floating PnL normalized to initial risk:
        R = profit / (|entry - initial_stop| * volume * point_value_lot)
    — the same formula as backtest/metrics.py::compute_r_metrics.
    Returns None when the risk base is unknown/degenerate (never invent R)."""
    if initial_stop is None or entry_price is None:
        return None
    risk_money = abs(float(entry_price) - float(initial_stop)) * float(volume) * float(point_value_lot)
    if risk_money <= 1e-12:
        return None
    return float(profit) / risk_money


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _fmt_money(x: float) -> str:
    return f"{float(x):+,.2f}"


def _fmt_price(x) -> str:
    """Compact price: no thousands separators (matter for grep-ability and for
    exact-match assertions), trailing zeros stripped."""
    try:
        return f"{float(x):.5f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(x)


def _fmt_conf(c) -> str:
    """Confidence is stored as a 0..1 fraction (RealtimePipeline); display %.
    Tolerate already-percent values (> 1) so a future pipeline change cannot
    produce absurd output like 7300%."""
    if c is None:
        return "n/a"
    try:
        c = float(c)
    except (TypeError, ValueError):
        return str(c)
    pct = c * 100.0 if -1.0 <= c <= 1.0 else c
    return f"{pct:.1f}%"


def _fmt_duration(seconds) -> str:
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "n/a"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _fmt_price_list(values) -> str:
    if not values:
        return "n/a"
    try:
        return " / ".join(_fmt_price(v) for v in values)
    except TypeError:
        return str(values)


# ---------------------------------------------------------------------------
# /status — open positions with floating PnL in $ and R
# ---------------------------------------------------------------------------

def format_position_line(pos, contexts: dict, cfg: dict,
                         now: Optional[datetime] = None) -> str:
    """One open position as a multi-line status block."""
    now = now or datetime.now(UTC)
    sym2asset = symbol_to_asset_map(cfg)
    symbol = getattr(pos, "symbol", "?")
    asset_key = sym2asset.get(symbol)
    label = f"{asset_key} ({symbol})" if asset_key else symbol
    ptype = int(getattr(pos, "type", 0))
    direction = "BUY 🟢" if ptype == 0 else "SELL 🔴"
    volume = getattr(pos, "volume", 0.0)
    entry = getattr(pos, "price_open", None)
    current = getattr(pos, "price_current", None)
    profit = float(getattr(pos, "profit", 0.0))
    ticket = getattr(pos, "ticket", "?")

    ctx = contexts.get(str(ticket)) or {}
    initial_stop = ctx.get("invalidation")
    pvl = point_value_lot_for(cfg, asset_key)
    r = floating_r(profit, entry, initial_stop, volume, pvl)
    r_txt = f"{r:+.2f} R" if r is not None else "R: n/a (нет контекста входа)"

    opened_ts = getattr(pos, "time", None)
    age_txt = "n/a"
    if opened_ts:
        try:
            age_txt = _fmt_duration(now.timestamp() - float(opened_ts))
        except (TypeError, ValueError, OSError):
            age_txt = "n/a"

    regime = ctx.get("regime") or "n/a"

    lines = [
        f"{direction} {label} — {volume} лот | #{ticket}",
        f"  Вход: {_fmt_price(entry)} → сейчас: {_fmt_price(current)}",
        f"  P&L: ${_fmt_money(profit)} | {r_txt}",
        f"  В сделке: {age_txt} | Режим при входе: {regime}",
    ]
    return "\n".join(lines)


def format_positions_report(positions, contexts: dict, cfg: dict,
                            now: Optional[datetime] = None) -> str:
    """The positions section shared by /status."""
    now = now or datetime.now(UTC)
    if not positions:
        return "📭 Открытых позиций нет."
    lines = [f"📂 Открытые позиции ({len(positions)}):"]
    for pos in positions:
        lines.append(format_position_line(pos, contexts, cfg, now=now))
    return "\n".join(lines)


def format_status_report(info, positions, contexts: dict, cfg: dict,
                         dry_run: bool = False, n_assets: int = 0,
                         now: Optional[datetime] = None) -> str:
    """Full /status: trader mode + account header (the pre-existing summary)
    followed by the per-position detail with floating PnL in $ and R."""
    now = now or datetime.now(UTC)
    mode = "⏸ DRY-RUN (на паузе)" if dry_run else "▶️ LIVE"
    lines = [f"📊 Статус трейдера — {mode} (UTC {now:%Y-%m-%d %H:%M})"]
    if info is not None:
        floating = float(getattr(info, "equity", 0.0)) - float(getattr(info, "balance", 0.0))
        lines.append(
            f"💼 Баланс: ${float(getattr(info, 'balance', 0.0)):,.2f} | "
            f"Equity: ${float(getattr(info, 'equity', 0.0)):,.2f} | "
            f"Плавающий P&L: ${_fmt_money(floating)}"
        )
    else:
        lines.append("💼 Данные счёта недоступны (account_info вернул None).")
    if n_assets:
        lines.append(f"Активов в пайплайне: {n_assets}")
    lines.append("")
    lines.append(format_positions_report(positions, contexts, cfg, now=now))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /why <ASSET> — why did we enter this position
# ---------------------------------------------------------------------------

CONTEXT_UNAVAILABLE = (
    "⚠️ Позиция найдена, но контекст входа недоступен: записи в "
    "logs/live_positions.json нет (позиция могла быть открыта до появления "
    "журналирования, при перезапуске без записи или вручную). "
    "Причину входа восстановить нельзя — выдумывать её не буду."
)

CONTEXT_HELP = (
    "ℹ️ Контекст входа (почему открыта позиция) сохраняется в "
    "logs/live_positions.json при открытии позиции (см. execution/mt5_trader.py)."
)


def format_why_report(asset_key: str, mt5_symbol: str, position,
                      context: Optional[dict]) -> str:
    """Answer "why are we in this trade" verbatim from the recorded entry
    context. Never fabricates a reason: missing context -> explicit notice."""
    if position is None:
        return f"📭 Нет открытой позиции по {asset_key} ({mt5_symbol})."

    ticket = getattr(position, "ticket", "?")
    ptype = int(getattr(position, "type", 0))
    direction = "BUY 🟢" if ptype == 0 else "SELL 🔴"
    header = (
        f"🧠 Почему открыта позиция {asset_key} ({mt5_symbol}) #{ticket}\n"
        f"{direction} {getattr(position, 'volume', '?')} лот | "
        f"Вход: {_fmt_price(getattr(position, 'price_open', None))} | "
        f"P&L: ${_fmt_money(getattr(position, 'profit', 0.0))}"
    )

    if not context:
        return f"{header}\n\n{CONTEXT_UNAVAILABLE}\n{CONTEXT_HELP}"

    reasoning = context.get("reasoning_summary") or "(reasoning_summary пуст)"

    # --- Readable entry-parameter breakdown (owner request 2026-08-11) ---
    bias = context.get("bias", "n/a")
    bias_word = {"long": "КУПЛЯ (покупка, ставка на рост)", "short": "ПРОДАЖА (ставка на снижение)"}.get(str(bias).lower(), str(bias))
    entry_zone = context.get("entry_zone")
    invalidation = context.get("invalidation")
    targets = context.get("targets") or []
    entry_ref = entry_zone[1] if entry_zone else context.get("entry_price")
    inv_txt = _fmt_price(invalidation) if invalidation is not None else "н/д"
    tgt_txt = " / ".join(_fmt_price(t) for t in targets) if targets else "н/д"

    # Risk:reward per level (price distance), plain-language.
    rr_lines = []
    if invalidation is not None and entry_ref is not None:
        try:
            risk = abs(float(entry_ref) - float(invalidation))
            if risk > 0:
                rr_lines.append(
                    f"• Риск (до стопа): ≈{_fmt_price(risk)} пунктов"
                )
                for i, t in enumerate(targets[:3], start=1):
                    reward = abs(float(t) - float(entry_ref))
                    rr_lines.append(
                        f"• Цель TP{i}: {_fmt_price(t)}  →  прибыль ≈ {reward / risk:.2f} к риску (на 1 ед. риска)"
                    )
        except (TypeError, ValueError):
            rr_lines = []

    session_map = {
        "asia": "азиатская сессия (обычно спокойная, низкая ликвидность)",
        "london": "лондонская сессия (высокая ликвидность, старт европейской торговли)",
        "new_york": "нью-йоркская сессия (пик ликвидности, пересечение с Европой)",
        "off_session": "вне торговых сессий (низкая ликвидность)",
    }
    session_txt = session_map.get(str(context.get("session", "")).lower(), context.get("session", "н/д"))
    regime_txt = {
        "trend_up": "восходящий тренд",
        "trend_down": "нисходящий тренд",
        "range": "боковик/диапазон",
        "compression": "сжатие волатильности",
        "reversal_watch": "потенциальный разворот",
    }.get(str(context.get("regime", "")).lower(), context.get("regime", "н/д"))

    conf = context.get("confidence")
    conf_txt = _fmt_conf(conf)
    conf_hint = ""
    if conf is not None:
        try:
            c = float(conf)
            pct = c * 100.0 if -1.0 <= c <= 1.0 else c
            conf_hint = " (сигнал достаточно уверенный)" if pct >= 60 else " (уверенность средняя — торгуйте осторожнее)"
        except (TypeError, ValueError):
            pass

    lines = [
        header,
        "",
        "НАПРАВЛЕНИЕ",
        f"• {bias_word}  — уверенность модели: {conf_txt}{conf_hint}",
        "",
        "КОНТЕКСТ РЫНКА",
        f"• Режим рынка: {regime_txt}",
        f"• Торговая сессия: {session_txt}",
        f"• Дословная причина из сигнала: «{reasoning}»",
        "",
        "ПЛАН ВХОДА / ВЫХОДА",
        f"• Зона входа: {_fmt_price_list(entry_zone)}",
        f"• Стоп-лосс (защита от убытка): {inv_txt}",
        f"• Цели прибыли (TP1/TP2/TP3): {tgt_txt}",
    ]
    if rr_lines:
        lines.append("")
        lines.append("РИСК / ПРИБЫЛЬ")
        lines.extend(rr_lines)
    lines.append("")
    lines.append("• Шаг сетки (ATR-шаг): " + _fmt_price(context.get("step")) if context.get("step") is not None else "")
    lines.append(f"• Открыта (UTC): {context.get('opened_at_utc', 'n/a')}")
    lines.append("")
    lines.append("Простыми словами: модель оценила, что движение в сторону входа вероятнее, чем обратное; стоп защищает от убытка, а цели фиксируют прибыль по мере движения цены.")
    # Drop any empty trailing lines gracefully
    return "\n".join([l for l in lines if l])


# ---------------------------------------------------------------------------
# Deals fetching (works against the real terminal AND the bundled shim)
# ---------------------------------------------------------------------------

def fetch_deals_between(dt_from: datetime, dt_to: datetime) -> list:
    """history_deals_get for a UTC datetime window, filtered client-side.

    Real MetaTrader5 accepts (date_from, date_to) positionally. The bundled
    simulation shim only supports history_deals_get(position=...) and binds the
    first positional arg to `position`, so on the shim we fall back to fetching
    everything and filtering by deal time ourselves. Read-only either way."""
    m = _load_mt5()
    if m is None:
        return []
    ts_from, ts_to = int(dt_from.timestamp()), int(dt_to.timestamp())

    deals = None
    try:
        deals = m.history_deals_get(dt_from, dt_to)
    except Exception as exc:
        logger.info("history_deals_get(from, to) unsupported here: %s", exc)
        deals = None

    if deals is None and hasattr(m, "_inject"):
        # Simulation shim: no date-range support — pull all deals, filter below.
        try:
            deals = m.history_deals_get()
        except Exception as exc:
            logger.warning("history_deals_get() failed: %s", exc)
            deals = None

    out = []
    for d in (deals or []):
        try:
            t = int(getattr(d, "time", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ts_from <= t <= ts_to:
            out.append(d)
    return out


def realized_pnl_today(now: Optional[datetime] = None) -> float:
    """Sum of profit+swap+commission over all of today's (UTC) deals — every
    cash movement booked today, matching how mt5_trader totals closed PnL."""
    now = now or datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deals = fetch_deals_between(day_start, now)
    return float(sum(
        float(getattr(d, "profit", 0.0) or 0.0)
        + float(getattr(d, "swap", 0.0) or 0.0)
        + float(getattr(d, "commission", 0.0) or 0.0)
        for d in deals
    ))


# ---------------------------------------------------------------------------
# /metrics [today|week] — closed-trade statistics
# ---------------------------------------------------------------------------

PERIODS = {
    "today": "сегодня (UTC)",
    "week": "последние 7 дней (UTC)",
    "2week": "последние 14 дней (UTC)",
    "month": "последние 30 дней (UTC)",
    "3month": "последние 90 дней (UTC)",
    "all": "вся история (UTC)",
}
# Which /metrics period keys exist (ordered for /help).
PERIOD_KEYS = ["today", "week", "2week", "month", "3month", "all"]


def period_range(kind: str, now: Optional[datetime] = None):
    """(dt_from, dt_to, human_label) for a /metrics period key."""
    now = now or datetime.now(UTC)
    days = {
        "week": 7,
        "2week": 14,
        "month": 30,
        "3month": 90,
    }.get(kind)
    if days is not None:
        return now - timedelta(days=days), now, PERIODS[kind]
    if kind == "all":
        return None, now, PERIODS["all"]
    return now.replace(hour=0, minute=0, second=0, microsecond=0), now, PERIODS["today"]


def compute_deal_metrics(deals, contexts: Optional[dict] = None,
                         cfg: Optional[dict] = None) -> dict:
    """Closed-trade statistics over exit deals (any non-IN deal realizes PnL,
    so partial TP closes count as separate realizations — documented behaviour).

    Returns {n, wins, losses, win_rate_pct, profit_factor, expectancy,
    total_pnl, mean_r, n_r}. R is computed only where the initial stop is known
    (entry context still present for the position, e.g. partial closes) — the
    context is purged once a position fully closes, so R is often n/a for old
    trades; we never invent it.
    """
    exits = [d for d in (deals or []) if int(getattr(d, "entry", 0) or 0) != _DEAL_ENTRY_IN]
    pnls = [
        float(getattr(d, "profit", 0.0) or 0.0)
        + float(getattr(d, "swap", 0.0) or 0.0)
        + float(getattr(d, "commission", 0.0) or 0.0)
        for d in exits
    ]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = float(sum(wins))
    gross_loss = float(-sum(losses))
    if gross_loss > 1e-12:
        profit_factor: Optional[float] = gross_profit / gross_loss
    elif gross_profit > 1e-12:
        profit_factor = float("inf")
    else:
        profit_factor = None
    total_pnl = float(sum(pnls))
    expectancy = total_pnl / n if n else 0.0

    r_values = []
    if contexts and cfg is not None:
        sym2asset = symbol_to_asset_map(cfg)
        in_price_by_pos = {}
        for d in deals or []:
            if int(getattr(d, "entry", 0) or 0) == _DEAL_ENTRY_IN:
                in_price_by_pos[getattr(d, "position_id", None)] = getattr(d, "price", None)
        for d, pnl in zip(exits, pnls):
            ctx = contexts.get(str(getattr(d, "position_id", ""))) or {}
            invalidation = ctx.get("invalidation")
            entry_price = in_price_by_pos.get(getattr(d, "position_id", None))
            asset_key = ctx.get("asset_key") or sym2asset.get(getattr(d, "symbol", ""))
            r = floating_r(pnl, entry_price, invalidation,
                           getattr(d, "volume", 0.0),
                           point_value_lot_for(cfg, asset_key))
            if r is not None:
                r_values.append(r)

    # Extended statistics (owner request 2026-08-11, detailed per-period stats).
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    # Max consecutive losses + max drawdown of cumulative PnL.
    max_consec_losses = 0
    run = 0
    cum = 0.0
    running_max = float("-inf")
    max_dd = 0.0
    for p in pnls:
        if p < 0:
            run += 1
            max_consec_losses = max(max_consec_losses, run)
        else:
            run = 0
        cum += p
        running_max = max(running_max, cum)
        max_dd = min(max_dd, cum - running_max)

    best = max(pnls) if n else 0.0
    worst = min(pnls) if n else 0.0

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (100.0 * len(wins) / n) if n else 0.0,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_consec_losses": max_consec_losses,
        "max_drawdown": max_dd,
        "best_trade": best,
        "worst_trade": worst,
        "mean_r": (sum(r_values) / len(r_values)) if r_values else None,
        "n_r": len(r_values),
    }


def _plural_deals(n: int) -> str:
    """Russian pluralization for "сделка": 1 сделка, 3 сделки, 5 сделок."""
    if n % 100 in (11, 12, 13, 14):
        return "сделок"
    return {1: "сделка", 2: "сделки", 3: "сделки", 4: "сделки"}.get(n % 10, "сделок")


def _fmt_metrics_line(label: str, m: dict) -> str:
    pf = m["profit_factor"]
    pf_txt = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
    r_txt = f" | Exp: {m['mean_r']:+.2f} R (n={m['n_r']})" if m.get("mean_r") is not None else ""
    return (
        f"{label}: {m['n']} {_plural_deals(m['n'])} | WR {m['win_rate_pct']:.1f}% | PF {pf_txt} | "
        f"Exp: ${_fmt_money(m['expectancy'])}{r_txt} | P&L: ${_fmt_money(m['total_pnl'])}"
    )


def _fmt_metrics_detail(m: dict) -> str:
    """Extended detail block for one metrics bucket (owner request 2026-08-11)."""
    pf = m["profit_factor"]
    pf_txt = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
    r_txt = f"{m['mean_r']:+.2f} R (n={m['n_r']})" if m.get("mean_r") is not None else "n/a"
    return "\n".join([
        f"  Сделок: {m['n']} | Побед: {m['wins']} | Поражений: {m['losses']}",
        f"  Win rate: {m['win_rate_pct']:.1f}% | Profit factor: {pf_txt}",
        f"  Средний выигрыш: ${_fmt_money(m['avg_win'])} | Средний убыток: ${_fmt_money(m['avg_loss'])}",
        f"  Expectancy: ${_fmt_money(m['expectancy'])} | Средний R: {r_txt}",
        f"  Лучшая сделка: ${_fmt_money(m['best_trade'])} | Худшая: ${_fmt_money(m['worst_trade'])}",
        f"  Макс. подряд убытков: {m['max_consec_losses']} | Макс. просадка: ${_fmt_money(m['max_drawdown'])}",
        f"  Итоговый P&L: ${_fmt_money(m['total_pnl'])}",
    ])


def format_metrics_report(deals, contexts: dict, cfg: dict, period_label: str) -> str:
    """/metrics <period>: per-asset and total closed-trade statistics (detailed)."""
    total = compute_deal_metrics(deals, contexts=contexts, cfg=cfg)
    header = f"📈 Метрики закрытых сделок — {period_label}"
    if total["n"] == 0:
        return f"{header}\nЗакрытых сделок за период нет."

    sym2asset = symbol_to_asset_map(cfg)
    by_asset: dict = {}
    for d in deals or []:
        symbol = getattr(d, "symbol", "?")
        label = sym2asset.get(symbol)
        label = f"{label} ({symbol})" if label else symbol
        by_asset.setdefault(label, []).append(d)

    lines = [header, "━━━━━━━━━━━━", "Σ ВСЕГО"]
    lines.append(_fmt_metrics_detail(total))
    for label in sorted(by_asset):
        m = compute_deal_metrics(by_asset[label], contexts=contexts, cfg=cfg)
        lines.append("━━━━━━━━━━━━")
        lines.append(f"• {label}")
        lines.append(_fmt_metrics_detail(m))
    lines.append("━━━━━━━━━━━━")
    lines.append("Сделка = закрывающий (OUT) дил; частичные TP учитываются отдельными реализациями.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /account — account state
# ---------------------------------------------------------------------------

def format_account_report(info, realized_today: float,
                          now: Optional[datetime] = None) -> str:
    """Balance/equity/margin snapshot + realized PnL for today (UTC)."""
    now = now or datetime.now(UTC)
    if info is None:
        return "❌ Данные счёта недоступны (account_info вернул None — терминал не подключён?)."
    balance = float(getattr(info, "balance", 0.0))
    equity = float(getattr(info, "equity", 0.0))
    margin = float(getattr(info, "margin", 0.0))
    margin_free = float(getattr(info, "margin_free", 0.0))
    margin_level = float(getattr(info, "margin_level", 0.0) or 0.0)
    floating = equity - balance
    ml_txt = f"{margin_level:,.1f}%" if margin > 1e-12 else "— (нет открытой маржи)"
    return "\n".join([
        f"💼 Счёт (UTC {now:%Y-%m-%d %H:%M})",
        f"Баланс: ${balance:,.2f}",
        f"Equity: ${equity:,.2f}",
        f"Плавающий P&L: ${_fmt_money(floating)}",
        f"Реализованный P&L за сегодня: ${_fmt_money(realized_today)}",
        f"Маржа: ${margin:,.2f} | Свободная маржа: ${margin_free:,.2f}",
        f"Уровень маржи: {ml_txt}",
    ])
