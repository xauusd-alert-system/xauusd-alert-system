"""
Netting partial-close flow for the MT5 TradeGroup executor
(P1.5.1 §12–§15). P2-1.3.

Pure extraction from ``execution.mt5_trade_group``: the idempotent
volume-aware netting close, the deterministic close-volume computation from
the broker's ACTUAL position and the cumulative-allocation ledger, and the
post-close volume-ledger update live here as module-level functions taking
the executor as their first argument. The executor keeps thin delegating
methods.

Behavior is unchanged: the close actionId is recorded only AFTER a successful
close (restart safety, ТЗ §29) and a below-step close is noted ONCE and the
position stays managed.
"""
from __future__ import annotations

from typing import Any

from data.trade_group_store import (
    has_action,
    load_group,
    mark_action,
    update_group_state,
)
from execution.mt5_common import BrokerUnavailable, MT5BrokerContext
from execution.reconciliation import emit_execution_error
from execution.trade_group import TradeGroupSpec, floor_to_step, new_leg_id


def floor_to_step_value(value: float, step: float) -> float:
    # P0-6: delegate to the shared Decimal(str(x)) ROUND_DOWN helper —
    # float division produced dust like 0.009999999999999998/0.01 -> 0.
    return floor_to_step(value, step)


def netting_close_leg(executor, group: dict[str, Any], leg: int,
                      action_id: str) -> bool:
    """Idempotent, volume-aware netting partial close (P1.5.1 §12–§15).

    The close volume is computed from the BROKER's actual position volume
    and the cumulative-allocation ledger — never from the initial requested
    volume. The actionId is recorded only AFTER a successful close, so a
    rejected close is retried on the next poll and a successful close is
    never re-sent (restart safety, ТЗ §29).
    """
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    if has_action(executor.db_path, group_id, action_id):
        return False
    close_volume = netting_close_volume(executor, group, leg)
    if close_volume <= 0.0:
        # increment below the broker volume_step/min: nothing fillable.
        # Note it ONCE (actionId-guarded) and keep the position managed.
        if mark_action(executor.db_path, group_id, f"{action_id}-SKIP",
                       {"reason": "below_volume_step", "leg": leg}):
            emit_execution_error(
                executor.ledger_db_path, spec,
                reason=f"{action_id} below volume_step (no fillable close)",
                payload={"action_id": action_id, "leg": leg, "mode": spec.mode},
                leg=leg,
            )
        return False
    driver = executor._resolve_driver(spec)
    ref = new_leg_id(group_id, 1)  # aggregate position ref
    result = driver.close_partial(ref, close_volume)
    if result.get("status") != "filled":
        emit_execution_error(executor.ledger_db_path, spec,
                             reason=f"{action_id} rejected",
                             payload={"action_id": action_id,
                                      "retcode": result.get("retcode"),
                                      "comment": result.get("comment"),
                                      "mode": spec.mode},
                             leg=leg)
        return False
    filled_close = float(result.get("filled_volume") or close_volume)
    mark_action(executor.db_path, group_id, action_id,
                {"leg": leg, "volume": close_volume,
                 "filled_volume": filled_close,
                 "fill_price": result.get("fill_price")})
    update_volume_after_close(executor, group, leg, filled_close)
    fill_price = float(result.get("fill_price") or 0.0)
    if leg == 1:
        executor._tp1_filled(group, fill_price, broker_closed=False)
    elif leg == 2:
        executor._tp2_filled(group, fill_price, broker_closed=False)
    else:
        executor._tp3_filled(group, fill_price, broker_closed=False)
    return True


def netting_close_volume(executor, group: dict[str, Any], leg: int) -> float:
    """P1.5.1 §12–§15: deterministic close volume from the broker's ACTUAL
    position volume and the cumulative-allocation volume ledger."""
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    driver = executor._resolve_driver(spec)
    try:
        snapshot = MT5BrokerContext(executor.mt5, magic=executor.magic) \
            .symbol_snapshot(spec.broker_symbol)
    except BrokerUnavailable:
        return 0.0
    step = float(snapshot.get("volume_step") or 0.0)
    volume_min = float(snapshot.get("volume_min") or 0.0)
    pos = driver.query_position(new_leg_id(group_id, 1))
    if pos is None:
        return 0.0
    remaining_before = float(pos.get("volume") or 0.0)
    if remaining_before <= 0.0:
        return 0.0
    if leg == 3:
        # TP3 closes the ENTIRE remaining broker volume (§15)
        return floor_to_step_value(remaining_before, step)
    volume = group.get("volume") or {}
    total_filled = float(volume.get("total_filled") or 0.0)
    already_closed = float(volume.get("total_closed") or 0.0)
    cumulative = sum(float(t.allocation) for t in spec.targets if t.leg <= leg)
    desired_cumulative = total_filled * cumulative
    desired_increment = desired_cumulative - already_closed
    close_volume = min(desired_increment, remaining_before)
    # P0-6: the ledger difference above carries float dust
    # (0.03 * 1/3 == 0.009999999999999998); a step*1e-9 epsilon preserves
    # the intended lot count while floor_to_step itself stays pure
    # ROUND_DOWN (P0-6 dust test relies on the pure behaviour).
    close_volume = floor_to_step_value(close_volume + step * 1e-9, step)
    if close_volume > 0.0 and close_volume < volume_min - 1e-9:
        # partial close below broker volume_min is unfillable (a FULL close
        # of the remaining volume is handled by the leg==3 branch)
        return 0.0
    return close_volume


def update_volume_after_close(executor, group: dict[str, Any], leg: int,
                              filled_close: float) -> None:
    """Update the per-group/per-leg volume ledger after a confirmed close."""
    group_id = group["group_id"]
    volume = dict(group.get("volume") or {})
    volume["total_closed"] = round(
        float(volume.get("total_closed") or 0.0) + filled_close, 8)
    volume["total_remaining"] = round(
        float(volume.get("total_filled") or 0.0) - volume["total_closed"], 8)
    legs = volume.setdefault("legs", {})
    entry = legs.setdefault(str(leg), {})
    entry["closed_volume"] = round(
        float(entry.get("closed_volume") or 0.0) + filled_close, 8)
    entry["remaining_volume"] = round(
        float(entry.get("filled_volume") or 0.0) - entry["closed_volume"], 8)
    current = load_group(executor.db_path, group_id)
    update_group_state(executor.db_path, group_id, current["state"], volume=volume)
