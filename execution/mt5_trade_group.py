"""
MT5 TradeGroup demo executor (ТЗ P1.5).

Connects ``TradeGroupSpec v1`` to a REAL MetaTrader 5 terminal — demo accounts
only. The pipeline:

    TradeGroupSpec -> ExecutionIntent -> account capability detection
    -> broker constraint validation -> order submission -> broker fills
    -> OPENED -> TP1 -> confirmed BE on residual -> TP2 -> TP3/STOP
    -> reconciliation

Hard gates (ТЗ §2/§38/§39):

* ``mode=live``            -> ``LiveExecutionForbidden`` (always).
* ``mode=demo``            -> requires ``TRADE_GROUP_ENABLE_DEMO=1`` AND the
                              connected account ``trade_mode == DEMO``
                              (``DemoAccountRequired`` otherwise).
* ``mode=paper``           -> delegated to the existing paper ``TradeGroupExecutor``.
* account margin mode ``unknown`` -> ``AccountModeUnknown`` (never guessed).
* deployment mode ``live_systematic`` -> ``ExecutionForbidden``.

The executor NEVER recomputes TP/SL: levels come from the immutable spec
(``MT5BrokerContext.validate_geometry`` aligns to tick only and rejects with
``ORDER_GEOMETRY_INVALID`` instead of stretching). Every broker action is
idempotent via ``actionId`` (store.mark_action), so restart/reconciliation can
never duplicate an order, a partial close or a BE modify.

``poll_once()`` is the reconciliation-driven state machine: it reads fresh
broker positions/deals and advances groups; local price is only a CANDIDATE
trigger for netting partial closes — every state change waits for broker
confirmation (order result / deal / position query).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from data.trade_group_store import (
    has_action,
    list_groups,
    load_group,
    mark_action,
    save_group,
    update_group_state,
)
from data.trading_event_ledger import append_trading_event
from execution.execution_intent import ExecutionIntent, ExecutionIntentMismatch
from execution.mt5_common import (
    ACCOUNT_MODE_UNKNOWN,
    AccountModeUnknown,
    BrokerUnavailable,
    MT5BrokerContext,
)
from execution.mt5_hedging_adapter import MT5HedgingDriver
from execution.mt5_netting_adapter import MT5NettingDriver
from execution.reconciliation import (
    classify_broker_close,
    detect_orphan_positions,
    emit_execution_error,
    inspect_group,
    latest_out_deal,
)
from execution.trade_geometry import (
    CostSnapshot,
    GeometryRejected,
    compute_break_even,
)
from execution.trade_group import (
    BeStatus,
    GroupState,
    TradeGroupSpec,
    check_group_not_expired,
    check_group_risk,
    new_leg_id,
    require_transition,
)
from execution.trade_group_executor import (
    DemoExecutionNotEnabled,
    DuplicateSubmissionError,
    LiveExecutionForbidden,
    PaperDriver,
    TradeGroupExecutor,
)

ORDER_GEOMETRY_INVALID = "ORDER_GEOMETRY_INVALID"


class ExecutionForbidden(RuntimeError):
    """Deployment/account conditions forbid execution."""


class DemoAccountRequired(ExecutionForbidden):
    """mode=demo requires the connected MT5 account to be a DEMO account."""


class PaperModeDelegated(RuntimeError):
    """mode=paper groups are handled by the paper TradeGroupExecutor."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class MT5TradeGroupExecutor:
    """Demo MT5 group executor: submit + poll/reconcile lifecycle."""

    def __init__(
        self,
        db_path: str,
        *,
        ledger_db_path: str | None = None,
        mt5=None,
        driver=None,
        allow_demo: bool | None = None,
        max_be_retries: int = 3,
        cost: CostSnapshot | None = None,
        notifier: Callable[[str], None] | None = None,
        magic: int = 777111,
        deployment_mode: str | None = None,
        be_close_tolerance: float = 0.0,
    ):
        self.db_path = db_path
        self.ledger_db_path = ledger_db_path or db_path
        self.mt5 = mt5
        self.notifier = notifier
        self.magic = int(magic)
        self.max_be_retries = max(1, int(max_be_retries))
        self.cost = cost or CostSnapshot()
        self.be_close_tolerance = float(be_close_tolerance)
        self.deployment_mode = deployment_mode
        import os
        if allow_demo is None:
            allow_demo = os.environ.get("TRADE_GROUP_ENABLE_DEMO", "0") == "1"
        self.allow_demo = bool(allow_demo)
        self.driver = driver
        self._driver_cache: dict[str, Any] = {}
        self._paper: TradeGroupExecutor | None = None

    # ------------------------------------------------------------------
    # Mode / account gates (ТЗ §2/§38/§39)
    # ------------------------------------------------------------------

    def _gate_mode(self, spec: TradeGroupSpec) -> None:
        if spec.mode == "live":
            raise LiveExecutionForbidden(
                "live trade-group execution is forbidden until explicit "
                "P2 live promotion approval"
            )
        if spec.mode == "paper":
            return  # delegated below
        if not self.allow_demo:
            raise DemoExecutionNotEnabled(
                "demo trade-group execution requires TRADE_GROUP_ENABLE_DEMO=1"
            )
        if self.deployment_mode == "live_systematic":
            raise ExecutionForbidden(
                "deployment.mode=live_systematic forbids demo trade-group execution"
            )

    def _require_demo_account(self) -> dict[str, Any]:
        """ТЗ §38: the connected account must be a DEMO account."""
        if self.mt5 is None:
            raise ExecutionForbidden("MT5 terminal is not available")
        ctx = MT5BrokerContext(self.mt5, magic=self.magic)
        account = ctx.account_info()
        demo = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        if demo is None or account["trade_mode"] != demo:
            raise DemoAccountRequired(
                f"connected account (login={account['login']}, "
                f"trade_mode={account['trade_mode']}) is not a DEMO account"
            )
        return account

    def _resolve_driver(self, spec: TradeGroupSpec):
        """Hedging/netting driver by fresh account detection; never guessed.

        Drivers are memoized per account mode so virtual-leg ref maps survive
        across polls (a fresh driver per poll would lose the netting ref map).
        """
        if self.driver is not None:
            return self.driver
        if self.mt5 is None:
            raise ExecutionForbidden("MT5 terminal is not available")
        ctx = MT5BrokerContext(self.mt5, magic=self.magic)
        mode = ctx.account_mode()
        if mode == "unknown":
            raise AccountModeUnknown(ACCOUNT_MODE_UNKNOWN)
        if mode not in self._driver_cache:
            if mode == "hedging":
                self._driver_cache[mode] = MT5HedgingDriver(self.mt5, magic=self.magic)
            else:
                self._driver_cache[mode] = MT5NettingDriver(self.mt5, magic=self.magic)
        return self._driver_cache[mode]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_group(self, spec: TradeGroupSpec) -> GroupState:
        """Register a validated spec (paper: delegate to the paper executor)."""
        self._gate_mode(spec)
        if spec.mode == "paper":
            if self._paper is None:
                self._paper = TradeGroupExecutor(
                    self.db_path, ledger_db_path=self.ledger_db_path,
                    driver=PaperDriver(), cost=self.cost,
                )
            return self._paper.create_group(spec)
        require_transition(GroupState.DRAFT, GroupState.VALIDATED)
        save_group(self.db_path, spec, state=GroupState.VALIDATED)
        append_trading_event(
            self.ledger_db_path, event_type="signal_validated",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=spec.group_id,
            payload={"geometry": spec.as_geometry_payload(), "state": "VALIDATED",
                     "mode": spec.mode},
        )
        return GroupState.VALIDATED

    def submit_group(self, group_id: str) -> GroupState:
        """VALIDATED -> SUBMITTED with broker submission (demo path).

        Performs, in order (ТЗ §6/§9/§31/§32): demo-account gate, account-mode
        detection, fresh broker snapshot + constraint validation, intent
        geometry-hash verification, expiry + risk re-check, then idempotent leg
        submission (OPEN-L1/L2/L3 actionIds).
        """
        current = load_group(self.db_path, group_id)
        if current is None:
            raise DuplicateSubmissionError(f"group {group_id} not found")
        spec: TradeGroupSpec = current["spec"]
        self._gate_mode(spec)
        if spec.mode == "paper":
            if self._paper is None:
                raise RuntimeError("paper executor not initialized")
            return self._paper.submit_group(group_id)
        require_transition(current["state"], GroupState.SUBMITTED)

        account = self._require_demo_account()
        driver = self._resolve_driver(spec)

        # --- ExecutionIntent + geometry verification (ТЗ §6) ----------------
        intent = ExecutionIntent.from_spec(spec)
        try:
            intent.require_geometry_unchanged(spec)
        except ExecutionIntentMismatch as exc:
            self._reject_group(spec, "EXECUTION_INTENT_MISMATCH", str(exc))
            raise

        # --- broker constraint validation (ТЗ §9: never stretch) -------------
        ctx = MT5BrokerContext(self.mt5, magic=self.magic)
        try:
            ctx.validate_geometry(spec, spec.broker_symbol)
        except GeometryRejected as exc:
            self._reject_group(spec, exc.reason_code, exc.detail)
            raise
        except BrokerUnavailable as exc:
            self._reject_group(spec, "BROKER_UNAVAILABLE", str(exc))
            raise

        # --- expiry / risk re-check with FRESH balance (ТЗ §31/§32) ---------
        if not check_group_not_expired(spec.expires_at_utc_ms, _now_ms()):
            self._reject_group(spec, "SIGNAL_EXPIRED", "signal TTL expired before submission")
            return GroupState.REJECTED
        ok, reason = check_group_risk(
            spec.risk.estimated_loss_at_sl, spec.risk.max_cash,
            spec.risk.max_pct, account["balance"],
        )
        if not ok:
            self._reject_group(spec, reason or "RISK_LIMIT_EXCEEDED",
                               "group risk re-check failed at submission")
            return GroupState.REJECTED

        if not mark_action(self.db_path, group_id, "OPEN-GROUP",
                           {"intent_id": intent.intent_id,
                            "geometry_hash": intent.geometry_hash}):
            raise DuplicateSubmissionError(
                f"group {group_id} was already submitted (action OPEN-GROUP); "
                f"restart recovery must not resubmit"
            )

        volumes = intent.leg_volumes
        legs = []
        broker_ids = {}
        for leg, volume in zip((1, 2, 3), volumes):
            result = driver.submit_leg(spec, leg, volume)
            broker_ids[new_leg_id(group_id, leg)] = result
            status = result.get("status", "rejected")
            if status == "filled":
                leg_state = "SUBMITTED"
                self._append_leg_event(spec, "leg_submitted", leg, result, volume)
            elif status == "virtual":
                leg_state = "VIRTUAL"
                self._append_leg_event(spec, "leg_submitted", leg, result, volume)
            elif status == "partially_filled":
                leg_state = "PARTIALLY_FILLED"
                self._append_leg_event(spec, "leg_submitted", leg, result, volume)
                self._append_leg_event(spec, "leg_partially_filled", leg, result, volume)
            else:
                leg_state = "REJECTED"
                self._append_leg_event(spec, "leg_rejected", leg, result, volume)
            legs.append({
                "leg": leg, "price": spec.leg_price(leg), "volume": volume,
                "state": leg_state, "broker": result,
            })

        update_group_state(self.db_path, group_id, GroupState.SUBMITTED,
                           legs=legs, broker_ids=broker_ids,
                           be_state=current.get("be_state"))
        save_group(self.db_path, spec, state=GroupState.SUBMITTED,
                   legs=legs, broker_ids=broker_ids, submitted=True,
                   be_state=current.get("be_state"),
                   intent_json=intent.model_dump_json(),
                   account_mode=driver.account_mode)
        append_trading_event(
            self.ledger_db_path, event_type="group_submitted",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=spec.group_id,
            payload={"account_mode": driver.account_mode, "legs": legs,
                     "geometry": spec.as_geometry_payload(),
                     "mode": spec.mode, "intent_id": intent.intent_id},
        )
        return GroupState.SUBMITTED

    def _reject_group(self, spec: TradeGroupSpec, reason_code: str, detail: str) -> None:
        require_transition(GroupState.VALIDATED, GroupState.REJECTED)
        save_group(self.db_path, spec, state=GroupState.REJECTED, submitted=False)
        append_trading_event(
            self.ledger_db_path, event_type="group_rejected",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason=reason_code, group_id=spec.group_id,
            payload={"reason_code": reason_code, "detail": detail, "mode": spec.mode},
        )

    def _append_leg_event(self, spec: TradeGroupSpec, event_type: str, leg: int,
                          result: dict[str, Any], requested_volume: float) -> None:
        append_trading_event(
            self.ledger_db_path, event_type=event_type,
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason=str(result.get("comment", "") or "") or None,
            group_id=spec.group_id, leg_id=new_leg_id(spec.group_id, leg),
            payload={
                "broker_order_id": result.get("order_id"),
                "broker_position_id": result.get("position_id"),
                "broker_deal_id": result.get("deal_id"),
                "requested_volume": requested_volume,
                "filled_volume": result.get("filled_volume"),
                "fill_price": result.get("fill_price"),
                "retcode": result.get("retcode"),
                "mode": spec.mode,
            },
        )

    # ------------------------------------------------------------------
    # Poll / reconcile state machine (ТЗ §15/§21/§22/§23)
    # ------------------------------------------------------------------

    def poll_once(self) -> list[str]:
        """Advance all non-terminal demo groups from fresh broker state.

        Returns the list of events emitted this pass. Local price is a
        candidate trigger only; every transition is confirmed by broker
        result/deal/position query.
        """
        events: list[str] = []
        for group in list_groups(self.db_path):
            spec = group["spec"]
            if spec.mode != "demo":
                continue
            if group["state"] in (GroupState.RECONCILED, GroupState.STOPPED,
                                  GroupState.REJECTED, GroupState.EXPIRED,
                                  GroupState.CANCELLED, GroupState.FAILED):
                continue
            events.extend(self._advance_group(group))
        return events

    def _advance_group(self, group: dict[str, Any]) -> list[str]:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        state = group["state"]
        driver = self._resolve_driver(spec)
        ctx = MT5BrokerContext(self.mt5, magic=self.magic)
        inspection = inspect_group(driver, group)
        events: list[str] = []

        # --- broker-side closure first (TP or SL closed by the broker) -------
        # Deals are CONSUMED once (actionId "DEAL-<ticket>"): re-polling the
        # same history can never re-fire a transition (ТЗ §28/§30).
        unconsumed = [
            deal for deal in sorted(
                inspection.closed_out_deals,
                key=lambda d: int(d.get("time", 0)), reverse=True,
            )
            if not has_action(self.db_path, group_id, f"DEAL-{deal.get('ticket')}")
        ]
        if unconsumed:
            out_deal = unconsumed[0]
            kind = classify_broker_close(spec, out_deal, self.be_close_tolerance)
            if kind == "stop":
                mark_action(self.db_path, group_id, f"DEAL-{out_deal.get('ticket')}",
                            {"kind": "stop", "price": out_deal.get("price")})
                return [self._stop_group(group, float(out_deal["price"]))]
            if kind == "tp1":
                if state in (GroupState.SUBMITTED, GroupState.OPENED):
                    mark_action(self.db_path, group_id, f"DEAL-{out_deal.get('ticket')}",
                                {"kind": "tp1", "price": out_deal.get("price")})
                    self._tp1_filled(group, float(out_deal["price"]), broker_closed=True)
                    events.append("tp1_filled")
                    state = GroupState.TP1_FILLED
            elif kind == "tp2":
                # strict: TP2 after BE_CONFIRMED only (a TP2 deal observed while
                # still TP1_FILLED is picked up on the NEXT poll after BE
                # confirms — the deal stays in history, nothing is lost)
                if state == GroupState.BE_CONFIRMED:
                    mark_action(self.db_path, group_id, f"DEAL-{out_deal.get('ticket')}",
                                {"kind": "tp2", "price": out_deal.get("price")})
                    self._tp2_filled(group, float(out_deal["price"]), broker_closed=True)
                    events.append("tp2_filled")
                    state = GroupState.TP2_FILLED
            elif kind == "tp3":
                if state == GroupState.TP2_FILLED:
                    mark_action(self.db_path, group_id, f"DEAL-{out_deal.get('ticket')}",
                                {"kind": "tp3", "price": out_deal.get("price")})
                    self._tp3_filled(group, float(out_deal["price"]), broker_closed=True)
                    events.append("tp3_filled")
                    state = GroupState.RECONCILED

        # --- fills while SUBMITTED (hedging: positions appear) ---------------
        if state == GroupState.SUBMITTED:
            rejected = [item for item in group.get("legs", [])
                        if item.get("state") == "REJECTED"]
            if rejected:
                # ТЗ §12 partial submission: deterministic controlled recovery —
                # any rejected leg at open fails the group; the accepted legs
                # keep their broker ids so reconciliation still tracks them
                require_transition(state, GroupState.FAILED)
                update_group_state(self.db_path, group_id, GroupState.FAILED)
                emit_execution_error(
                    self.ledger_db_path, spec,
                    reason="partial submission: rejected leg(s)",
                    payload={"rejected_legs": [i.get("leg") for i in rejected],
                             "mode": spec.mode},
                )
                events.append("group_failed")
                return events
            filled = [item for item in group.get("legs", [])
                      if item.get("state") in ("SUBMITTED", "PARTIALLY_FILLED", "VIRTUAL")]
            if filled:
                self._open_group(group, inspection)
                events.append("group_opened")
                state = GroupState.OPENED

        # --- netting: candidate-triggered partial closes (ТЗ §15/§14) --------
        if driver.account_mode == "netting" and state in (
                GroupState.OPENED, GroupState.BE_CONFIRMED, GroupState.TP2_FILLED):
            try:
                tick = ctx.symbol_snapshot(spec.broker_symbol)
            except BrokerUnavailable:
                return events
            direction = 1.0 if spec.side == "long" else -1.0
            price = tick["bid"] if spec.side == "long" else tick["ask"]
            if state == GroupState.OPENED and direction * (price - spec.geometry.tp1) >= 0.0:
                volume = round(spec.risk.total_volume * spec.leg_allocation(1), 8)
                if self._netting_close_leg(group, 1, volume, "CLOSE-TP1"):
                    events.append("tp1_filled")
                    state = GroupState.TP1_FILLED
            elif state == GroupState.BE_CONFIRMED and \
                    direction * (price - spec.geometry.tp2) >= 0.0:
                volume = round(spec.risk.total_volume * spec.leg_allocation(2), 8)
                if self._netting_close_leg(group, 2, volume, "CLOSE-TP2"):
                    events.append("tp2_filled")
                    state = GroupState.TP2_FILLED
            elif state == GroupState.TP2_FILLED and \
                    direction * (price - spec.geometry.tp3) >= 0.0:
                volume = round(spec.risk.total_volume * spec.leg_allocation(3), 8)
                if self._netting_close_leg(group, 3, volume, "CLOSE-TP3"):
                    events.append("tp3_filled")
                    state = GroupState.RECONCILED

        # --- BE flow: TP1_FILLED -> BE_REQUESTED -> BE_CONFIRMED/BE_RETRY ----
        if state == GroupState.TP1_FILLED:
            # reload: the group dict predates the TP1 transition (netting
            # partial close or broker close in this same poll)
            group = load_group(self.db_path, group_id)
            events.append(self._request_be(group))
            state = GroupState.BE_REQUESTED
        if state in (GroupState.BE_REQUESTED, GroupState.BE_RETRY):
            # reload so be_state (requested_price) is fresh after _request_be
            group = load_group(self.db_path, group_id)
            result = self._verify_be(group)
            if result == "confirmed":
                events.append("be_confirmed")
                state = GroupState.BE_CONFIRMED
            elif result == "retry":
                events.append("be_retry")
                state = GroupState.BE_RETRY
            else:  # failed
                events.append("be_failed")
                state = GroupState.FAILED

        # --- orphan detection (ТЗ §28) ---------------------------------------
        detect_orphan_positions(driver, self.db_path,
                                ledger_db_path=self.ledger_db_path)
        return events

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _open_group(self, group: dict[str, Any], inspection) -> None:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        require_transition(group["state"], GroupState.OPENED)
        fill_price = None
        for pos in inspection.positions:
            fill_price = float(pos.get("price_open") or 0.0) or fill_price
        legs = group.get("legs", [])
        for item in legs:
            if item.get("state") in ("SUBMITTED", "PARTIALLY_FILLED", "VIRTUAL"):
                item["state"] = "OPEN"
        if fill_price is not None and spec.entry.actual_fill is None:
            spec = spec.with_actual_fill(fill_price)
        update_group_state(self.db_path, group_id, GroupState.OPENED, legs=legs)
        save_group(self.db_path, spec, state=GroupState.OPENED, legs=legs,
                   broker_ids=group.get("broker_ids", {}), submitted=True,
                   be_state=group.get("be_state"),
                   intent_json=group.get("intent_json"),
                   account_mode=group.get("account_mode"))
        append_trading_event(
            self.ledger_db_path, event_type="group_opened",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=group_id,
            payload={"actual_fill": spec.entry.actual_fill, "mode": spec.mode},
        )
        if self.notifier:
            self.notifier(self._opened_message(spec))

    def _tp1_filled(self, group: dict[str, Any], fill_price: float,
                    broker_closed: bool = False) -> None:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        state = group["state"]
        if state == GroupState.SUBMITTED:
            require_transition(state, GroupState.OPENED)
            update_group_state(self.db_path, group_id, GroupState.OPENED)
        require_transition(GroupState.OPENED, GroupState.TP1_FILLED)
        if spec.entry.actual_fill is None:
            spec = spec.with_actual_fill(fill_price)
        legs = group.get("legs", [])
        for item in legs:
            if item["leg"] == 1:
                item["state"] = "CLOSED"
                item["fill_price"] = fill_price
        update_group_state(self.db_path, group_id, GroupState.TP1_FILLED, legs=legs)
        save_group(self.db_path, spec, state=GroupState.TP1_FILLED, legs=legs,
                   broker_ids=group.get("broker_ids", {}), submitted=True,
                   be_state=group.get("be_state"),
                   intent_json=group.get("intent_json"),
                   account_mode=group.get("account_mode"))
        append_trading_event(
            self.ledger_db_path, event_type="tp1_filled",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason="broker_confirmed" if broker_closed else "partial_close_confirmed",
            group_id=group_id, leg_id=new_leg_id(group_id, 1),
            payload={"fill_price": fill_price, "entry_actual_fill": spec.entry.actual_fill,
                     "mode": spec.mode},
        )
        if self.notifier:
            self.notifier(self._tp1_message(spec))

    def _netting_close_leg(self, group: dict[str, Any], leg: int, volume: float,
                           action_id: str) -> bool:
        """Idempotent netting partial close; True only on broker confirmation.

        The actionId is recorded only AFTER a successful close, so a rejected
        close is retried on the next poll (deterministic recovery, ТЗ §26);
        a successful close is never re-sent (restart safety, ТЗ §29)."""
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        if has_action(self.db_path, group_id, action_id):
            return False
        driver = self._resolve_driver(spec)
        ref = new_leg_id(group_id, 1)  # aggregate position ref
        result = driver.close_partial(ref, volume)
        if result.get("status") != "filled":
            emit_execution_error(self.ledger_db_path, spec,
                                 reason=f"{action_id} rejected",
                                 payload={"action_id": action_id,
                                          "retcode": result.get("retcode"),
                                          "comment": result.get("comment"),
                                          "mode": spec.mode},
                                 leg=leg)
            return False
        mark_action(self.db_path, group_id, action_id,
                    {"leg": leg, "volume": volume, "fill_price": result.get("fill_price")})
        fill_price = float(result.get("fill_price") or 0.0)
        if leg == 1:
            self._tp1_filled(group, fill_price, broker_closed=False)
        elif leg == 2:
            self._tp2_filled(group, fill_price, broker_closed=False)
        else:
            self._tp3_filled(group, fill_price, broker_closed=False)
        return True

    def _tp2_filled(self, group: dict[str, Any], fill_price: float,
                    broker_closed: bool = False) -> None:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        require_transition(group["state"], GroupState.TP2_FILLED)
        legs = group.get("legs", [])
        for item in legs:
            if item["leg"] == 2:
                item["state"] = "CLOSED"
                item["fill_price"] = fill_price
        update_group_state(self.db_path, group_id, GroupState.TP2_FILLED, legs=legs)
        append_trading_event(
            self.ledger_db_path, event_type="tp2_filled",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason="broker_confirmed" if broker_closed else "partial_close_confirmed",
            group_id=group_id, leg_id=new_leg_id(group_id, 2),
            payload={"fill_price": fill_price, "mode": spec.mode,
                     "tp2": spec.geometry.tp2},
        )
        if self.notifier:
            self.notifier(self._tp_message(spec, "TP2", "✅ TP2 FILLED"))

    def _tp3_filled(self, group: dict[str, Any], fill_price: float,
                    broker_closed: bool = False) -> None:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        require_transition(group["state"], GroupState.TP3_FILLED)
        legs = group.get("legs", [])
        for item in legs:
            if item["leg"] == 3:
                item["state"] = "CLOSED"
                item["fill_price"] = fill_price
        update_group_state(self.db_path, group_id, GroupState.TP3_FILLED, legs=legs)
        update_group_state(self.db_path, group_id, GroupState.RECONCILED, legs=legs)
        append_trading_event(
            self.ledger_db_path, event_type="tp3_filled",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason="broker_confirmed" if broker_closed else "partial_close_confirmed",
            group_id=group_id, leg_id=new_leg_id(group_id, 3),
            payload={"fill_price": fill_price, "mode": spec.mode,
                     "tp3": spec.geometry.tp3},
        )
        append_trading_event(
            self.ledger_db_path, event_type="group_reconciled",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=group_id,
            payload={"mode": spec.mode, "geometry": spec.as_geometry_payload()},
        )
        if self.notifier:
            self.notifier(self._tp_message(spec, "TP3", "✅ TP3 FILLED"))

    def _stop_group(self, group: dict[str, Any], stop_price: float) -> str:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        require_transition(group["state"], GroupState.STOPPED)
        update_group_state(self.db_path, group_id, GroupState.STOPPED)
        append_trading_event(
            self.ledger_db_path, event_type="stop_filled",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason="broker_confirmed", group_id=group_id,
            payload={"stop_price": stop_price, "mode": spec.mode,
                     "sl": spec.geometry.sl},
        )
        if self.notifier:
            self.notifier(self._stopped_message(spec))
        return "stop_filled"

    # ------------------------------------------------------------------
    # BE flow (ТЗ §17–§20): actual fill, all apply_to legs, broker query
    # ------------------------------------------------------------------

    def _request_be(self, group: dict[str, Any]) -> str:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        require_transition(group["state"], GroupState.BE_REQUESTED)
        if spec.entry.actual_fill is None:
            raise ExecutionForbidden(
                "break-even requires a confirmed actual fill"
            )
        be = compute_break_even(side=spec.side, actual_fill=spec.entry.actual_fill,
                                cost=self.cost,
                                broker=MT5BrokerContext(self.mt5, magic=self.magic)
                                .broker_snapshot(spec.broker_symbol))
        be_state = dict(group.get("be_state") or {})
        be_state.update({
            "status": BeStatus.BE_REQUESTED.value,
            "raw_price": be["raw_price"],
            "protected_price": be["protected_price"],
            "requested_price": be["protected_price"],
            "retries": int(be_state.get("retries", 0)),
        })
        update_group_state(self.db_path, group_id, GroupState.BE_REQUESTED,
                           be_state=be_state)
        append_trading_event(
            self.ledger_db_path, event_type="be_requested",
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

    def _verify_be(self, group: dict[str, Any]) -> str:
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
        driver = self._resolve_driver(spec)
        refs = [self._be_ref(group, leg, driver) for leg in spec.break_even.apply_to]
        for leg, ref in zip(spec.break_even.apply_to, refs):
            if ref is None:
                return self._be_retry(group, be_state,
                                      f"leg {leg} has no broker position to protect")
            action_id = f"BE-L{leg}" if driver.account_mode == "hedging" else "BE"
            observed = driver.query_sl(ref)
            if observed is not None and abs(observed - requested) <= 1e-9:
                # broker already protects this leg (this poll or a previous
                # process) -> record completion, never re-modify (ТЗ §29)
                mark_action(self.db_path, group_id, action_id,
                            {"leg": leg, "sl": requested})
                continue
            accepted, comment = driver.modify_sl(ref, requested)
            if not accepted:
                # NOT marked -> the modify is genuinely retried on the next
                # poll (bounded by be_retries) — a rejected modify is not a
                # completed action (ТЗ §20)
                return self._be_retry(group, be_state, comment)
            # success recorded BEFORE the verification pass so a lagging
            # broker query can never cause a duplicate SL modify
            mark_action(self.db_path, group_id, action_id,
                        {"leg": leg, "sl": requested})
        # verify ALL required refs via broker query
        for ref in refs:
            observed = driver.query_sl(ref)
            if observed is None or abs(observed - requested) > 1e-9:
                return self._be_retry(
                    group, be_state, f"SL query mismatch observed={observed}"
                )
        be_state.update({"status": BeStatus.BE_CONFIRMED.value,
                         "confirmed_price": requested, "last_error": None})
        update_group_state(self.db_path, group_id, GroupState.BE_CONFIRMED,
                           be_state=be_state)
        append_trading_event(
            self.ledger_db_path, event_type="be_confirmed",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=group_id,
            payload={"confirmed_price": requested, "raw_price": be_state.get("raw_price"),
                     "mode": spec.mode},
        )
        if self.notifier:
            self.notifier(self._be_message(spec, requested))
        return "confirmed"

    def _be_ref(self, group: dict[str, Any], leg: int, driver) -> str | None:
        """Broker reference for a BE modify: hedging uses the physical position
        ticket, netting uses the virtual-leg ref (resolved to the aggregate)."""
        if driver.account_mode == "hedging":
            for item in group.get("legs", []):
                if item["leg"] == leg:
                    ticket = int(item.get("broker", {}).get("position_id") or 0)
                    return str(ticket) if ticket else None
            return None
        return new_leg_id(group["group_id"], leg)

    def _be_retry(self, group: dict[str, Any], be_state: dict[str, Any],
                  error: str) -> str:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        retries = int(be_state.get("retries", 0)) + 1
        be_state.update({"status": BeStatus.BE_RETRY.value, "retries": retries,
                         "last_error": error})
        if retries >= self.max_be_retries:
            update_group_state(self.db_path, group_id, GroupState.FAILED,
                               be_state=be_state)
            append_trading_event(
                self.ledger_db_path, event_type="execution_error",
                signal_id=spec.signal_id, asset_key=spec.asset_key,
                strategy_version=spec.strategy_version, config_hash=spec.config_hash,
                model_hash=spec.model_hash, actor="mt5_trade_group_executor",
                reason="BE retries exhausted", group_id=group_id,
                payload={"retries": retries, "last_error": error, "mode": spec.mode},
            )
            return "failed"
        update_group_state(self.db_path, group_id, GroupState.BE_RETRY,
                           be_state=be_state)
        append_trading_event(
            self.ledger_db_path, event_type="be_retry",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason=error, group_id=group_id,
            payload={"retries": retries, "mode": spec.mode},
        )
        return "retry"

    # ------------------------------------------------------------------
    # Restart recovery (ТЗ §29)
    # ------------------------------------------------------------------

    def recover_after_restart(self, group_id: str) -> dict[str, Any]:
        """Load + reconcile; never re-sends entry/TP/BE actions (idempotency)."""
        group = load_group(self.db_path, group_id)
        if group is None:
            raise DuplicateSubmissionError(f"group {group_id} not found in store")
        spec = group["spec"]
        if spec.mode == "demo":
            self._require_demo_account()
        driver = self._resolve_driver(spec)
        inspection = inspect_group(driver, group)
        append_trading_event(
            self.ledger_db_path, event_type="group_reconciled",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason="restart_recovery", group_id=group_id,
            payload={"state": group["state"].value,
                     "submitted": group["submitted"], "mode": spec.mode,
                     "positions_found": len(inspection.positions),
                     "out_deals": len(inspection.closed_out_deals)},
        )
        return group

    # ------------------------------------------------------------------
    # Telegram messages (ТЗ §35/§36) — only broker-confirmed events
    # ------------------------------------------------------------------

    def _opened_message(self, spec: TradeGroupSpec) -> str:
        return (
            f"🔥 TRADE GROUP OPENED\n{spec.asset_key}\n"
            f"{'LONG' if spec.side == 'long' else 'SHORT'}\n\n"
            f"Group: {spec.group_id}\n"
            f"Entry: {spec.entry.actual_fill or spec.entry.reference}\n"
            f"TP1: {spec.geometry.tp1}\nTP2: {spec.geometry.tp2}\n"
            f"TP3: {spec.geometry.tp3}\nSL: {spec.geometry.sl}\n"
            f"Mode: DEMO"
        )

    def _tp1_message(self, spec: TradeGroupSpec) -> str:
        return (
            f"✅ TP1 FILLED\nGroup: {spec.group_id}\n\n"
            f"Leg 1: CLOSED\n\nBE requested for:\n"
            f"Leg {spec.break_even.apply_to[0]}\nLeg {spec.break_even.apply_to[1]}\n"
            f"Mode: DEMO"
        )

    def _tp_message(self, spec: TradeGroupSpec, label: str, header: str) -> str:
        return f"{header}\nGroup: {spec.group_id}\nMode: DEMO"

    def _be_message(self, spec: TradeGroupSpec, sl_price: float) -> str:
        return (
            f"🟢 BE CONFIRMED\nGroup: {spec.group_id}\n\n"
            f"SL remaining legs: {sl_price}\nMode: DEMO"
        )

    def _stopped_message(self, spec: TradeGroupSpec) -> str:
        return f"🛑 STOPPED\nGroup: {spec.group_id}\nMode: DEMO"
