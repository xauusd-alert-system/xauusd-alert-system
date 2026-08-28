"""
TP/SL leg-transition handlers for the MT5 TradeGroup executor. P2-1.4.

Pure extraction from ``execution.mt5_trade_group``: the broker-confirmed
leg-close transitions (TP1 -> TP2 -> TP3 -> RECONCILED and STOP) live here as
module-level functions taking the executor as their first argument. The
executor keeps thin delegating methods (``mt5_netting_close`` invokes these
transitions after a partial close).

Behavior is unchanged: every transition is state-guarded
(``require_transition``), every ledger event carries provenance
(source/side/evidence) and Telegram notifications fire only on
broker-confirmed events (ТЗ §35/§36).
"""
from __future__ import annotations

from typing import Any

from data.trade_group_store import save_group, update_group_state
from data.trading_event_ledger import append_trading_event
from execution.trade_group import GroupState, TradeGroupSpec, new_leg_id, require_transition


def tp1_filled(executor, group: dict[str, Any], fill_price: float,
               broker_closed: bool = False) -> None:
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    state = group["state"]
    if state == GroupState.SUBMITTED:
        require_transition(state, GroupState.OPENED)
        update_group_state(executor.db_path, group_id, GroupState.OPENED)
    require_transition(GroupState.OPENED, GroupState.TP1_FILLED)
    if spec.entry.actual_fill is None:
        spec = spec.with_actual_fill(fill_price)
    legs = group.get("legs", [])
    for item in legs:
        if item["leg"] == 1:
            item["state"] = "CLOSED"
            item["fill_price"] = fill_price
    update_group_state(executor.db_path, group_id, GroupState.TP1_FILLED, legs=legs)
    save_group(executor.db_path, spec, state=GroupState.TP1_FILLED, legs=legs,
               broker_ids=group.get("broker_ids", {}), submitted=True,
               be_state=group.get("be_state"),
               intent_json=group.get("intent_json"),
               account_mode=group.get("account_mode"))
    append_trading_event(
        executor.ledger_db_path, event_type="tp1_filled",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        reason="broker_confirmed" if broker_closed else "partial_close_confirmed",
        group_id=group_id, leg_id=new_leg_id(group_id, 1),
        source="mt5" if broker_closed else "simulator",
        source_type="deal" if broker_closed else "paper_driver",
        source_id=f"DEAL-{int(fill_price * 1000)}" if broker_closed else None,
        payload={"fill_price": fill_price, "entry_actual_fill": spec.entry.actual_fill,
                 "mode": spec.mode,
                 "evidence": "broker_deal" if broker_closed else "partial_close_result"},
    )
    if executor.notifier:
        executor.notifier(executor._tp1_message(spec))


def tp2_filled(executor, group: dict[str, Any], fill_price: float,
               broker_closed: bool = False) -> None:
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    require_transition(group["state"], GroupState.TP2_FILLED)
    legs = group.get("legs", [])
    for item in legs:
        if item["leg"] == 2:
            item["state"] = "CLOSED"
            item["fill_price"] = fill_price
    update_group_state(executor.db_path, group_id, GroupState.TP2_FILLED, legs=legs)
    append_trading_event(
        executor.ledger_db_path, event_type="tp2_filled",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        reason="broker_confirmed" if broker_closed else "partial_close_confirmed",
        group_id=group_id, leg_id=new_leg_id(group_id, 2),
        source="mt5" if broker_closed else "simulator",
        source_type="deal" if broker_closed else "paper_driver",
        source_id=f"DEAL-{int(fill_price * 1000)}" if broker_closed else None,
        payload={"fill_price": fill_price, "mode": spec.mode,
                 "tp2": spec.geometry.tp2,
                 "evidence": "broker_deal" if broker_closed else "partial_close_result"},
    )
    if executor.notifier:
        executor.notifier(executor._tp_message(spec, "TP2", "✅ TP2 FILLED"))


def tp3_filled(executor, group: dict[str, Any], fill_price: float,
               broker_closed: bool = False) -> None:
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    require_transition(group["state"], GroupState.TP3_FILLED)
    legs = group.get("legs", [])
    for item in legs:
        if item["leg"] == 3:
            item["state"] = "CLOSED"
            item["fill_price"] = fill_price
    update_group_state(executor.db_path, group_id, GroupState.TP3_FILLED, legs=legs)
    update_group_state(executor.db_path, group_id, GroupState.RECONCILED, legs=legs)
    append_trading_event(
        executor.ledger_db_path, event_type="tp3_filled",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        reason="broker_confirmed" if broker_closed else "partial_close_confirmed",
        group_id=group_id, leg_id=new_leg_id(group_id, 3),
        source="mt5" if broker_closed else "simulator",
        source_type="deal" if broker_closed else "paper_driver",
        source_id=f"DEAL-{int(fill_price * 1000)}" if broker_closed else None,
        payload={"fill_price": fill_price, "mode": spec.mode,
                 "tp3": spec.geometry.tp3,
                 "evidence": "broker_deal" if broker_closed else "partial_close_result"},
    )
    append_trading_event(
        executor.ledger_db_path, event_type="group_reconciled",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        group_id=group_id,
        payload={"mode": spec.mode, "geometry": spec.as_geometry_payload()},
    )
    if executor.notifier:
        executor.notifier(executor._tp_message(spec, "TP3", "✅ TP3 FILLED"))


def stop_group(executor, group: dict[str, Any], stop_price: float) -> str:
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    require_transition(group["state"], GroupState.STOPPED)
    update_group_state(executor.db_path, group_id, GroupState.STOPPED)
    append_trading_event(
        executor.ledger_db_path, event_type="stop_filled",
        signal_id=spec.signal_id, asset_key=spec.asset_key,
        strategy_version=spec.strategy_version, config_hash=spec.config_hash,
        model_hash=spec.model_hash, actor="mt5_trade_group_executor",
        reason="broker_confirmed", group_id=group_id,
        source="mt5", source_type="deal",
        source_id=f"DEAL-{int(stop_price * 1000)}",
        payload={"stop_price": stop_price, "mode": spec.mode,
                 "sl": spec.geometry.sl,
                 "evidence": "broker_deal"},
    )
    if executor.notifier:
        executor.notifier(executor._stopped_message(spec))
    return "stop_filled"
