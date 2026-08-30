"""
Compensation flow for the MT5 TradeGroup executor (P1.5.1 §5–§9). P2-1.2.

Pure extraction from ``execution.mt5_trade_group``: partial-submission
compensation (begin), broker confirmation with bounded retries (verify) and
the related helpers live here as module-level functions taking the executor as
their first argument. The executor keeps thin delegating methods.

Behavior is unchanged: COMPENSATE actionIds are marked ONLY after broker
confirmation (or "already closed" — the goal state), rejected closes stay
unmarked for the bounded retry, and FAILED_WITH_OPEN_RISK stays non-terminal
so reconciliation keeps polling.
"""

from __future__ import annotations

from typing import Any

from data.trade_group_store import (
    has_action,
    load_group,
    mark_action,
    update_group_state,
)
from data.trading_event_ledger import append_trading_event
from execution.trade_group import (
    GroupState,
    TradeGroupSpec,
    new_leg_id,
    require_transition,
)


def begin_compensation(executor, group: dict[str, Any], rejected: list[dict[str, Any]]) -> str:
    """SUBMITTED/PARTIAL_SUBMISSION -> compensation of already-opened legs.

    Every compensating close uses a deterministic actionId
    (``COMPENSATE-L<n>`` or ``COMPENSATE-GROUP``) through the existing
    ``mark_action`` idempotency store — a retry/restart can never send a
    duplicate close. Geometry is never touched (P1.5.1 §6).

    Returns ``compensation_requested`` when all closes were accepted (the
    broker confirmation is verified on a later poll) or
    ``compensation_failed`` when at least one close was rejected (group ->
    FAILED_WITH_OPEN_RISK, reconciliation stays active).
    """
    group_id = group["group_id"]
    spec: TradeGroupSpec = group["spec"]
    require_transition(group["state"], GroupState.PARTIAL_SUBMISSION)
    opened = [
        item for item in group.get("legs", []) if item.get("state") in ("SUBMITTED", "PARTIALLY_FILLED", "VIRTUAL")
    ]
    update_group_state(executor.db_path, group_id, GroupState.PARTIAL_SUBMISSION)
    append_trading_event(
        executor.ledger_db_path,
        event_type="partial_submission",
        signal_id=spec.signal_id,
        asset_key=spec.asset_key,
        strategy_version=spec.strategy_version,
        config_hash=spec.config_hash,
        model_hash=spec.model_hash,
        actor="mt5_trade_group_executor",
        group_id=group_id,
        payload={
            "opened_legs": [item["leg"] for item in opened],
            "rejected_legs": [item["leg"] for item in rejected],
            "mode": spec.mode,
        },
    )
    if executor.notifier:
        executor.notifier(
            executor._partial_submission_message(
                spec, [item["leg"] for item in opened], [item["leg"] for item in rejected]
            )
        )

    driver = executor._resolve_driver(spec)
    comp_state = {
        "status": "REQUESTED",
        "retries": int(group.get("comp_state", {}).get("retries", 0)),
        "legs": {},
    }
    failures: list[int | str] = []

    # Idempotency design (P1.5.1 §5): a COMPENSATE actionId is marked ONLY
    # after the broker confirms the close (or the position is already
    # gone — "position not found" IS the goal state). A rejected close is
    # left UNMARKED so the bounded retry can re-send it; the broker itself
    # is the backstop against duplicate closes (a second close of a
    # nonexistent position is a no-op).
    refs: list[tuple[str, str | None]] = []  # (ref, action_id)
    if driver.account_mode == "netting":
        refs.append((new_leg_id(group_id, 1), "COMPENSATE-GROUP"))
    else:
        for item in opened:
            ticket = int(item.get("broker", {}).get("position_id") or 0)
            if ticket:
                refs.append((str(ticket), f"COMPENSATE-L{item['leg']}"))

    volume = dict(group.get("volume") or {})
    closed_total = float(volume.get("total_closed") or 0.0)
    for ref, action_id in refs:
        if action_id is not None and has_action(executor.db_path, group_id, action_id):
            continue  # already successfully compensated (restart safety)
        pos = driver.query_position(ref)
        if pos is None:
            # already closed (or never opened): the goal is achieved
            if action_id is not None:
                mark_action(executor.db_path, group_id, action_id, {"reason": "already_closed"})
                comp_state["legs"][action_id] = {"status": "filled", "comment": "already_closed"}
            continue
        result = driver.close_position(ref, float(pos["volume"]))
        label = action_id if action_id is not None else "GROUP"
        if result.get("status") == "filled":
            if action_id is not None:
                mark_action(
                    executor.db_path,
                    group_id,
                    action_id,
                    {"leg": label, "volume": pos["volume"], "fill_price": result.get("fill_price")},
                )
            comp_state["legs"][label] = result
            # volume ledger: closed volume grows with the ACTUAL closed
            # volume of every compensated reference (§10/§11/§25)
            filled_close = float(result.get("filled_volume") or pos["volume"])
            closed_total = round(closed_total + filled_close, 8)
            if label.startswith("COMPENSATE-L"):
                leg_no = int(label.rsplit("L", 1)[1])
                legs_ledger = volume.setdefault("legs", {})
                entry = legs_ledger.setdefault(str(leg_no), {})
                entry["closed_volume"] = round(float(entry.get("closed_volume") or 0.0) + filled_close, 8)
                entry["remaining_volume"] = 0.0
        else:
            comp_state["legs"][label] = result  # rejected; UNMARKED -> retried
            failures.append(label)
    volume["total_closed"] = round(closed_total, 8)
    volume["total_remaining"] = round(float(volume.get("total_filled") or 0.0) - closed_total, 8)

    if failures:
        comp_state["status"] = "FAILED"
        update_group_state(executor.db_path, group_id, GroupState.FAILED_WITH_OPEN_RISK, comp_state=comp_state)
        record_compensation_failure(executor, spec, comp_state, failures, reason="compensation close rejected")
        return "compensation_failed"
    update_group_state(
        executor.db_path, group_id, GroupState.COMPENSATION_REQUESTED, comp_state=comp_state, volume=volume
    )
    append_trading_event(
        executor.ledger_db_path,
        event_type="compensation_requested",
        signal_id=spec.signal_id,
        asset_key=spec.asset_key,
        strategy_version=spec.strategy_version,
        config_hash=spec.config_hash,
        model_hash=spec.model_hash,
        actor="mt5_trade_group_executor",
        group_id=group_id,
        payload={
            "action_ids": list(comp_state["legs"].keys()),
            "rejected_legs": [item["leg"] for item in rejected],
            "mode": spec.mode,
        },
    )
    return "compensation_requested"


def verify_compensation(executor, group: dict[str, Any]) -> str:
    """COMPENSATION_REQUESTED/FAILED_WITH_OPEN_RISK -> broker confirmation.

    Phase 1 (retry): any compensation close that was REJECTED (left
    unmarked) is re-sent, bounded by ``max_compensation_retries``. A
    position that is already gone counts as closed (the broker itself is
    the duplicate-close backstop).
    Phase 2 (verify): a reference is confirmed closed only when its broker
    position no longer exists. When every reference is closed, the group
    moves COMPENSATION_CONFIRMED -> FAILED with open risk == 0. Unconfirmed
    references keep reconciliation active (bounded retries ->
    FAILED_WITH_OPEN_RISK, which is NON-terminal — P1.5.1 §8/§9).
    """
    group_id = group["group_id"]
    group = load_group(executor.db_path, group_id)
    spec: TradeGroupSpec = group["spec"]
    driver = executor._resolve_driver(spec)
    comp_state = dict(group.get("comp_state") or {})
    comp_legs = dict(comp_state.get("legs", {}) or {})
    retries = int(comp_state.get("retries", 0))

    # --- Phase 1: re-send rejected (unmarked) closes ---------------------
    rejected_refs = [ref for ref, result in comp_legs.items() if result.get("status") != "filled"]
    if rejected_refs:
        if retries >= executor.max_compensation_retries:
            comp_state["status"] = "FAILED"
            if not comp_state.get("recorded"):
                comp_state["recorded"] = True
                record_compensation_failure(
                    executor, spec, comp_state, rejected_refs, reason="compensation retries exhausted"
                )
            update_group_state(executor.db_path, group_id, GroupState.FAILED_WITH_OPEN_RISK, comp_state=comp_state)
            return "failed"
        retries += 1
        comp_state["retries"] = retries
        still_failed: list[str] = []
        for ref in rejected_refs:
            ticket = compensation_ticket(group, driver, ref)
            pos = driver.query_position(ticket) if ticket else None
            if pos is None:
                # already closed between polls -> goal achieved
                comp_legs[ref] = {"status": "filled", "comment": "already_closed"}
                mark_action(
                    executor.db_path, group_id, compensation_action_id(ref), {"reason": "already_closed_on_retry"}
                )
                continue
            result = driver.close_position(ticket, float(pos["volume"]))
            comp_legs[ref] = result
            if result.get("status") == "filled":
                mark_action(
                    executor.db_path,
                    group_id,
                    compensation_action_id(ref),
                    {"leg": ref, "volume": pos["volume"], "fill_price": result.get("fill_price")},
                )
            else:
                still_failed.append(ref)
        comp_state["legs"] = comp_legs
        if still_failed:
            comp_state["status"] = "FAILED"
            if not comp_state.get("recorded"):
                comp_state["recorded"] = True
                record_compensation_failure(
                    executor, spec, comp_state, still_failed, reason="compensation close rejected on retry"
                )
            update_group_state(executor.db_path, group_id, GroupState.FAILED_WITH_OPEN_RISK, comp_state=comp_state)
            return "failed"
        comp_state.pop("recorded", None)
        update_group_state(executor.db_path, group_id, GroupState.COMPENSATION_REQUESTED, comp_state=comp_state)

    # --- Phase 2: verify every marked reference is closed ----------------
    pending: list[str] = []
    for ref in comp_legs:
        ticket = compensation_ticket(group, driver, ref)
        pos = driver.query_position(ticket) if ticket else None
        if pos is not None:
            pending.append(ref)
    if pending:
        comp_state["retries"] = int(comp_state.get("retries", 0)) + 1
        comp_state["pending"] = pending
        if comp_state["retries"] >= executor.max_compensation_retries:
            comp_state["status"] = "FAILED"
            if not comp_state.get("recorded"):
                comp_state["recorded"] = True
                record_compensation_failure(
                    executor, spec, comp_state, pending, reason="compensation not confirmed within retry budget"
                )
            update_group_state(executor.db_path, group_id, GroupState.FAILED_WITH_OPEN_RISK, comp_state=comp_state)
            return "failed"
        comp_state["status"] = "REQUESTED"
        update_group_state(executor.db_path, group_id, GroupState.COMPENSATION_REQUESTED, comp_state=comp_state)
        return "pending"

    # every compensated reference is closed -> consume their OUT deals so
    # they are never re-classified as TP/SL (§19) and finalize the group
    consume_compensation_deals(executor, group, driver, comp_legs)
    volume = dict(group.get("volume") or {})
    volume["total_closed"] = round(float(volume.get("total_filled") or 0.0), 8)
    volume["total_remaining"] = 0.0
    for entry in volume.get("legs", {}).values():
        entry["remaining_volume"] = 0.0
    comp_state["status"] = "CONFIRMED"
    # confirmed from either COMPENSATION_REQUESTED or a retried
    # FAILED_WITH_OPEN_RISK (both transitions are allowed)
    require_transition(group["state"], GroupState.COMPENSATION_CONFIRMED)
    update_group_state(
        executor.db_path, group_id, GroupState.COMPENSATION_CONFIRMED, comp_state=comp_state, volume=volume
    )
    append_trading_event(
        executor.ledger_db_path,
        event_type="compensation_confirmed",
        signal_id=spec.signal_id,
        asset_key=spec.asset_key,
        strategy_version=spec.strategy_version,
        config_hash=spec.config_hash,
        model_hash=spec.model_hash,
        actor="mt5_trade_group_executor",
        group_id=group_id,
        payload={"open_risk": 0.0, "mode": spec.mode},
    )
    require_transition(GroupState.COMPENSATION_CONFIRMED, GroupState.FAILED)
    update_group_state(executor.db_path, group_id, GroupState.FAILED, comp_state=comp_state, volume=volume)
    if executor.notifier:
        reason = ", ".join(
            f"LEG{leg}_REJECTED"
            for leg in sorted(int(i["leg"]) for i in group.get("legs", []) if i.get("state") == "REJECTED")
        )
        executor.notifier(executor._failed_after_compensation_message(spec, reason))
    return "confirmed"


def compensation_action_id(ref: str) -> str:
    """COMPENSATE actionId for a comp_state leg key (keys ARE action ids)."""
    return ref


def compensation_ticket(group: dict[str, Any], driver, ref: str) -> str:
    """Resolve a comp_state reference to a broker position ticket."""
    group_id = group["group_id"]
    if ref == "COMPENSATE-GROUP":
        pos = driver.query_position(new_leg_id(group_id, 1))
        return str(pos["ticket"]) if pos else ""
    for item in group.get("legs", []):
        if ref == f"COMPENSATE-L{item['leg']}":
            ticket = int(item.get("broker", {}).get("position_id") or 0)
            return str(ticket) if ticket else ""
    return ""


def consume_compensation_deals(executor, group: dict[str, Any], driver, comp_legs: dict[str, Any]) -> None:
    """Mark the compensation OUT deals consumed (never TP/SL-classified)."""
    group_id = group["group_id"]
    tickets: list[int] = []
    if driver.account_mode == "netting":
        pos = driver.query_position(new_leg_id(group_id, 1))
        if pos is not None:
            tickets.append(int(pos["ticket"]))
    for item in group.get("legs", []):
        ticket = int(item.get("broker", {}).get("position_id") or 0)
        if ticket:
            tickets.append(ticket)
    for ticket in set(tickets):
        for deal in driver.query_deals(ticket):
            if int(deal.get("entry", -1)) == 1:  # OUT
                mark_action(executor.db_path, group_id, f"DEAL-{deal.get('ticket')}", {"kind": "compensation"})


def record_compensation_failure(
    executor, spec: TradeGroupSpec, comp_state: dict[str, Any], open_refs: list[Any], reason: str
) -> None:
    """FAILED_WITH_OPEN_RISK: explicit ledger facts + emergency Telegram.
    Reconciliation keeps polling this non-terminal state (P1.5.1 §8)."""
    append_trading_event(
        executor.ledger_db_path,
        event_type="compensation_failed",
        signal_id=spec.signal_id,
        asset_key=spec.asset_key,
        strategy_version=spec.strategy_version,
        config_hash=spec.config_hash,
        model_hash=spec.model_hash,
        actor="mt5_trade_group_executor",
        reason=reason,
        group_id=spec.group_id,
        payload={
            "open_refs": [str(r) for r in open_refs],
            "retries": int(comp_state.get("retries", 0)),
            "mode": spec.mode,
        },
    )
    append_trading_event(
        executor.ledger_db_path,
        event_type="failed_with_open_risk",
        signal_id=spec.signal_id,
        asset_key=spec.asset_key,
        strategy_version=spec.strategy_version,
        config_hash=spec.config_hash,
        model_hash=spec.model_hash,
        actor="mt5_trade_group_executor",
        reason=reason,
        group_id=spec.group_id,
        payload={"open_refs": [str(r) for r in open_refs], "mode": spec.mode},
    )
    if executor.notifier:
        executor.notifier(executor._open_risk_message(spec, open_refs))
