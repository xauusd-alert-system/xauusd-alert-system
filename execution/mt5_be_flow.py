"""
Break-even flow for the MT5 TradeGroup executor (ТЗ §17–§20). P2-1.1.

Pure extraction from ``execution.mt5_trade_group``: the BE request, broker
verification, ticket resolution and bounded-retry logic now live here as
module-level functions taking the executor as their first argument. The
executor keeps thin delegating methods (tests/external code may patch them).

Behavior is unchanged: every broker action stays idempotent via
``mark_action``, the broker SL is queried BEFORE any modify (restart-safe,
ТЗ §29) and a rejected modify is never marked as completed (ТЗ §20).
"""
from __future__ import annotations

from typing import Any

from data.trade_group_store import mark_action, update_group_state
from data.trading_event_ledger import append_trading_event
from execution.mt5_common import ExecutionForbidden, MT5BrokerContext
from execution.trade_geometry import compute_break_even
from execution.trade_group import (
    BeStatus,
    GroupState,
    TradeGroupSpec,
    new_leg_id,
    require_transition,
)


def request_be(executor, group: dict[str, Any]) -> str:
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    require_transition(group["state"], GroupState.BE_REQUESTED)
    if spec.entry.actual_fill is None:
        raise ExecutionForbidden(
            "break-even requires a confirmed actual fill"
        )
    be = compute_break_even(side=spec.side, actual_fill=spec.entry.actual_fill,
                            cost=executor.cost,
                            broker=MT5BrokerContext(executor.mt5, magic=executor.magic)
                            .broker_snapshot(spec.broker_symbol))
    be_state = dict(group.get("be_state") or {})
    be_state.update({
        "status": BeStatus.BE_REQUESTED.value,
        "raw_price": be["raw_price"],
        "protected_price": be["protected_price"],
        "requested_price": be["protected_price"],
        "retries": int(be_state.get("retries", 0)),
    })
    update_group_state(executor.db_path, group_id, GroupState.BE_REQUESTED,
                       be_state=be_state)
    append_trading_event(
        executor.ledger_db_path, event_type="be_requested",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        group_id=group_id,
        payload={"raw_price": be["raw_price"],
                 "protected_price": be["protected_price"],
                 "requested_price": be["protected_price"],
                 "apply_to": spec.break_even.apply_to, "mode": spec.mode},
    )
    return "be_requested"


def verify_be(executor, group: dict[str, Any]) -> str:
    """modify + query every required leg; all must confirm (ТЗ §18/§19).

    Restart-safe: the broker SL is queried FIRST; an already-correct SL is
    recorded as done (mark_action is idempotent) and never re-modified, so
    recovery after a restart cannot re-send a BE modify the broker already
    applied (ТЗ §29).
    """
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    be_state = dict(group.get("be_state") or {})
    requested = float(be_state.get("requested_price") or 0.0)
    if requested <= 0.0:
        raise ExecutionForbidden("no requested BE price; run _request_be first")
    driver = executor._resolve_driver(spec)
    refs = [be_ref(group, leg, driver) for leg in spec.break_even.apply_to]
    for leg, ref in zip(spec.break_even.apply_to, refs):
        if ref is None:
            return be_retry(executor, group, be_state,
                            f"leg {leg} has no broker position to protect")
        action_id = f"BE-L{leg}" if driver.account_mode == "hedging" else "BE"
        observed = driver.query_sl(ref)
        if observed is not None and abs(observed - requested) <= 1e-9:
            # broker already protects this leg (this poll or a previous
            # process) -> record completion, never re-modify (ТЗ §29)
            mark_action(executor.db_path, group_id, action_id,
                        {"leg": leg, "sl": requested})
            continue
        accepted, comment = driver.modify_sl(ref, requested)
        if not accepted:
            # NOT marked -> the modify is genuinely retried on the next
            # poll (bounded by be_retries) — a rejected modify is not a
            # completed action (ТЗ §20)
            return be_retry(executor, group, be_state, comment)
        # success recorded BEFORE the verification pass so a lagging
        # broker query can never cause a duplicate SL modify
        mark_action(executor.db_path, group_id, action_id,
                    {"leg": leg, "sl": requested})
    # verify ALL required refs via broker query
    for ref in refs:
        observed = driver.query_sl(ref)
        if observed is None or abs(observed - requested) > 1e-9:
            return be_retry(
                executor, group, be_state, f"SL query mismatch observed={observed}"
            )
    be_state.update({"status": BeStatus.BE_CONFIRMED.value,
                     "confirmed_price": requested, "last_error": None})
    update_group_state(executor.db_path, group_id, GroupState.BE_CONFIRMED,
                       be_state=be_state)
    append_trading_event(
        executor.ledger_db_path, event_type="be_confirmed",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        group_id=group_id,
        source="mt5", source_type="position",
        source_id=f"POSITION:{spec.group_id}:{int(requested * 1000)}",
        payload={"confirmed_price": requested, "raw_price": be_state.get("raw_price"),
                 "mode": spec.mode,
                 "evidence": "broker_position_query"},
    )
    if executor.notifier:
        executor.notifier(executor._be_message(spec, requested))
    return "confirmed"


def be_ref(group: dict[str, Any], leg: int, driver) -> str | None:
    """Broker reference for a BE modify: hedging uses the physical position
    ticket, netting uses the virtual-leg ref (resolved to the aggregate)."""
    if driver.account_mode == "hedging":
        for item in group.get("legs", []):
            if item["leg"] == leg:
                ticket = int(item.get("broker", {}).get("position_id") or 0)
                return str(ticket) if ticket else None
        return None
    return new_leg_id(group["group_id"], leg)


def be_retry(executor, group: dict[str, Any], be_state: dict[str, Any],
             error: str) -> str:
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    retries = int(be_state.get("retries", 0)) + 1
    be_state.update({"status": BeStatus.BE_RETRY.value, "retries": retries,
                     "last_error": error})
    if retries >= executor.max_be_retries:
        update_group_state(executor.db_path, group_id, GroupState.FAILED,
                           be_state=be_state)
        append_trading_event(
            executor.ledger_db_path, event_type="execution_error",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason="BE retries exhausted", group_id=group_id,
            payload={"retries": retries, "last_error": error, "mode": spec.mode},
        )
        return "failed"
    update_group_state(executor.db_path, group_id, GroupState.BE_RETRY,
                       be_state=be_state)
    append_trading_event(
        executor.ledger_db_path, event_type="be_retry",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        reason=error, group_id=group_id,
        payload={"retries": retries, "mode": spec.mode},
    )
    return "retry"
