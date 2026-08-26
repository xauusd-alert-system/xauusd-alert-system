"""Runner <-> StealthExecutionEngine bridge.

Keeps the stealth layer separable from the legacy runner path. Provides:

  * ``now_et(now)`` — convert a platform-local ``datetime`` to naive US
    Eastern time (correct across DST) for the engine's ET session windows.
  * ``build_engine(cfg)`` — construct a ``StealthExecutionEngine`` from the
    ``challenge`` config; returns ``None`` when stealth is disabled.
  * ``adapt_signal(sig)`` — convert the legacy runner ``Signal``
    (``challenge.strategy``) into the ``ORBSignal`` the engine's
    ``process_signal`` expects. The engine recomputes its own risk profile and
    ignores ``tp``; range/gap/prices are carried through with best-effort
    defaults.
  * ``execute_plan(conn, plan, qty)`` — turn an engine execution plan into the
    minimal connector calls the HashHedge DOM exposes (market order at the
    plan's sized qty, then ``modify_stop`` to the plan's stop).
  * ``execute_actions(conn, actions)`` — apply engine management actions
    (partial_close / modify_stop / close_position) through the connector.

This module never changes strategy logic — it only adapts shapes and calls the
same connector methods the legacy runner already uses. It is import-safe when
stealth is disabled (``build_engine`` returns None).
"""
from __future__ import annotations

import logging
from datetime import datetime

from challenge.connector import HashHedgeConnector
from challenge.orb_strategy import ORBSignal, ORBStrategy
from challenge.stealth.execution_engine import StealthExecutionEngine

logger = logging.getLogger("challenge.stealth_bridge")

# Best-effort defaults for fields the legacy Signal does not carry.
_FALLBACK_RANGE_PCT = 0.8
_FALLBACK_GAP_PCT = 0.0


def now_et(now: datetime) -> datetime:
    """Convert a UTC-anchored platform datetime to naive Eastern time.

    ``datetime.now()`` in this deployment produces a naive clock that tracks
    UTC wall time. ``ORBStrategy.et_offset_hours`` returns -4 EDT / -5 EST, so
    adding it shifts UTC -> Eastern (15:30 UTC + -4h = 11:30 ET) for the
    engine's 09:30-10:30 windows.
    """
    offset = ORBStrategy.et_offset_hours(now.date())  # -4 or -5
    return now.replace(tzinfo=None) + _timedelta_hours(offset)


def _timedelta_hours(h: int) -> "timedelta":
    from datetime import timedelta
    return timedelta(hours=h)


def build_engine(cfg: dict) -> StealthExecutionEngine | None:
    """Construct the stealth engine from the challenge config (or None)."""
    challenge_cfg = (cfg or {}).get("challenge")
    if not challenge_cfg or not bool(challenge_cfg.get("stealth", {}).get("enabled", False)):
        return None
    try:
        return StealthExecutionEngine(challenge_cfg)
    except Exception as exc:  # pragma: no cover - defensive, config-dependent
        logger.error("Stealth engine init failed: %s", exc)
        return None


def adapt_signal(sig) -> ORBSignal:
    """Convert a legacy runner Signal into the ORBSignal the engine expects.

    Fields the engine reads: symbol, bias, entry, stop, volume_ratio,
    range_pct. ``tp`` is ignored (the engine recomputes its own risk profile).
    Tries known attribute names so both the legacy ``Signal`` and the newer
    ``ORBSignal`` (already correct) round-trip unchanged.
    """
    symbol = getattr(sig, "symbol", "")
    bias = getattr(sig, "bias", "")
    entry = float(getattr(sig, "entry", 0.0))
    stop = float(getattr(sig, "stop", 0.0))
    tp = float(getattr(sig, "tp", 0.0))
    volume_ratio = float(getattr(sig, "volume_ratio", 0.0) or 0.0)
    range_pct = getattr(sig, "range_pct", None)
    if range_pct is None:
        range_pct = _FALLBACK_RANGE_PCT
    gap_pct = getattr(sig, "gap_pct", None)
    if gap_pct is None:
        gap_pct = _FALLBACK_GAP_PCT

    # range_high/low: derive from entry/stop when the legacy Signal has none.
    range_high = getattr(sig, "range_high", None)
    range_low = getattr(sig, "range_low", None)
    if range_high is None:
        range_high = entry if bias == "long" else stop
    if range_low is None:
        range_low = stop if bias == "long" else entry

    return ORBSignal(
        symbol=symbol,
        bias=bias,
        entry=entry,
        stop=stop,
        tp=tp,
        range_high=float(range_high),
        range_low=float(range_low),
        range_pct=float(range_pct),
        volume_ratio=float(volume_ratio),
        gap_pct=float(gap_pct),
        timestamp=getattr(sig, "timestamp", datetime.now()),
    )


def _humanized_sleep(seconds: float) -> None:
    """Best-effort humanized pre-action pause (never blocks the loop long)."""
    if seconds and seconds > 0:
        import time
        time.sleep(min(seconds, 60.0))


def execute_plan(conn: HashHedgeConnector, plan: dict) -> bool:
    """Place the order a plan describes via the connector.

    The engine's plan carries the sized qty and the humanized entry/stop/tp.
    HashHedge's DOM exposes a market order (place_order) + a stop modifier; TP
    is managed later by manage_position as the price runs. Applies the plan's
    ``delay`` as a humanized pause before submitting.
    """
    symbol = plan.get("symbol")
    bias = plan.get("bias")
    qty = int(plan.get("shares", 0))
    stop = plan.get("stop")
    if not symbol or not bias or qty < 1:
        logger.warning("execute_plan: bad plan keys symbol=%s bias=%s qty=%s", symbol, bias, qty)
        return False

    delay = float(plan.get("delay", 0.0))
    _humanized_sleep(delay)

    side = "buy" if bias == "long" else "sell"
    try:
        ok = conn.place_order(symbol, side, qty)
        if not ok:
            logger.warning("execute_plan: place_order rejected for %s", symbol)
            return False
        if stop and stop > 0:
            # Best-effort SL set; not fatal if the DOM slot differs.
            try:
                conn.modify_stop(symbol, float(stop))
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.warning("execute_plan: modify_stop failed for %s: %s", symbol, exc)
        logger.info("EXECUTED via engine plan: %s %s x%d @ %.2f (stop %.2f)",
                    bias.upper(), symbol, qty, plan.get("entry", 0), stop)
        return True
    except Exception as exc:  # noqa: BLE001 - connector may raise on DOM mapping
        logger.warning("execute_plan: order failed for %s: %s", symbol, exc)
        return False


def execute_actions(conn: HashHedgeConnector, position: dict, actions: list) -> None:
    """Apply an engine's management actions through the connector."""
    symbol = position.get("symbol", "")
    for action in actions:
        act = action.get("action")
        delay = float(action.get("delay", 0.0))
        _humanized_sleep(delay)
        try:
            if act == "modify_stop":
                new_stop = action.get("new_stop")
                if new_stop and new_stop > 0:
                    conn.modify_stop(symbol, float(new_stop))
                    logger.info("engine manage: SL -> %.2f for %s", new_stop, symbol)
            elif act == "partial_close":
                shares = int(action.get("shares", 0))
                if shares > 0:
                    conn.close_partial(symbol, shares)
                    logger.info("engine manage: partial close %d/%s for %s", shares,
                                position.get("qty"), symbol)
            elif act == "close_position":
                conn.close_position(symbol)
                logger.info("engine manage: close position %s", symbol)
            else:
                logger.debug("engine manage: unknown action %r", act)
        except Exception as exc:  # noqa: BLE001 - never take the loop down
            logger.warning("engine manage action %r failed for %s: %s", act, symbol, exc)