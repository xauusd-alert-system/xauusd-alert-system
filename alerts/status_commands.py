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
from datetime import datetime, timedelta, timezone
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
            import MetaTrader5

            _mt5 = MetaTrader5
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
    now = now or datetime.now(timezone.utc)
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
    now = now or datetime.now(timezone.utc)
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
    now = now or datetime.now(timezone.utc)
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
    lines = [
        header,
        "",
        "Причина входа (дословно из сигнала):",
        f"«{reasoning}»",
        "",
        f"Bias: {context.get('bias', 'n/a')} | Confidence: {_fmt_conf(context.get('confidence'))}",
        f"Regime: {context.get('regime', 'n/a')} | Session: {context.get('session', 'n/a')}",
        f"Entry zone: {_fmt_price_list(context.get('entry_zone'))}",
        f"Invalidation (стоп): {_fmt_price(context.get('invalidation'))}",
        f"Targets: {_fmt_price_list(context.get('targets'))}",
        f"Открыта (UTC): {context.get('opened_at_utc', 'n/a')}",
    ]
    return "\n".join(lines)


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
    now = now or datetime.now(timezone.utc)
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
}


def period_range(kind: str, now: Optional[datetime] = None):
    """(dt_from, dt_to, human_label) for a /metrics period key."""
    now = now or datetime.now(timezone.utc)
    if kind == "week":
        return now - timedelta(days=7), now, PERIODS["week"]
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

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (100.0 * len(wins) / n) if n else 0.0,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_pnl": total_pnl,
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


def format_metrics_report(deals, contexts: dict, cfg: dict, period_label: str) -> str:
    """/metrics <period>: per-asset and total closed-trade statistics."""
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

    lines = [header, _fmt_metrics_line("Σ Всего", total)]
    for label in sorted(by_asset):
        m = compute_deal_metrics(by_asset[label], contexts=contexts, cfg=cfg)
        lines.append(_fmt_metrics_line(f"  • {label}", m))
    lines.append("Сделка = закрывающий (OUT) дил; частичные TP учитываются отдельными реализациями.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /account — account state
# ---------------------------------------------------------------------------

def format_account_report(info, realized_today: float,
                          now: Optional[datetime] = None) -> str:
    """Balance/equity/margin snapshot + realized PnL for today (UTC)."""
    now = now or datetime.now(timezone.utc)
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
