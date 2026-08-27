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

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# P0-4: |reference - fill| / reference above this logs a WARNING (the fill is
# still accepted if within the spec's hard deviation gate, but drifted enough
# that the operator should notice slippage).
FILL_DRIFT_WARN_RATIO = 0.001

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
    floor_to_step,
    new_leg_id,
    require_transition,
)
from execution import telegram_formatter as tf
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


def _metrics_record(metric: str, value: int = 1, **extra: Any) -> None:
    """ТЗ 6.1: fail-open metrics hook (execution must never depend on it)."""
    try:
        from monitoring.metrics import get_collector

        get_collector().record(metric, value, **extra)
    except Exception:  # noqa: BLE001 — observability must not break trading
        pass


def _metrics_timing(stage: str, duration_ms: float, **extra: Any) -> None:
    """ТЗ 6.1: fail-open per-stage timing hook."""
    try:
        from monitoring.metrics import get_collector

        get_collector().record_timing(stage, duration_ms, **extra)
    except Exception:  # noqa: BLE001
        pass


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
        max_compensation_retries: int = 3,
    ):
        self.db_path = db_path
        self.ledger_db_path = ledger_db_path or db_path
        self.mt5 = mt5
        self.notifier = notifier
        self.magic = int(magic)
        self.max_be_retries = max(1, int(max_be_retries))
        self.max_compensation_retries = max(1, int(max_compensation_retries))
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
        _metrics_record("groups_created", asset_key=spec.asset_key)
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

        # P1.6 §41/§42: execution gate — the approved spec must carry the full
        # provenance lineage (market/feature/model/profile/broker/cost + both
        # hashes). A spec without provable parents is rejected, never executed.
        try:
            spec.require_execution_provenance()
        except ValueError as exc:
            self._reject_group(spec, "PROVENANCE_INVALID", str(exc))
            raise

        account = self._require_demo_account()
        driver = self._resolve_driver(spec)

        # --- ExecutionIntent + geometry verification (ТЗ §6) ----------------
        intent = ExecutionIntent.from_spec(spec)
        try:
            intent.require_geometry_unchanged(spec)
            intent.require_provenance_present(spec)
        except ExecutionIntentMismatch as exc:
            self._reject_group(spec, "PROVENANCE_INVALID", str(exc))
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
        # P1.5.1 §10/§11: per-group + per-leg volume ledger (source of truth for
        # every netting close and hedging leg management).
        volume_ledger = {
            "total_requested": float(spec.risk.total_volume),
            "total_filled": 0.0,
            "total_closed": 0.0,
            "total_remaining": 0.0,
            "legs": {
                str(leg): {
                    "requested_volume": float(v), "filled_volume": 0.0,
                    "closed_volume": 0.0, "remaining_volume": 0.0,
                }
                for leg, v in zip((1, 2, 3), volumes)
            },
        }
        for leg, volume in zip((1, 2, 3), volumes):
            result = driver.submit_leg(spec, leg, volume)
            broker_ids[new_leg_id(group_id, leg)] = result
            status = result.get("status", "rejected")
            filled = float(result.get("filled_volume") or 0.0)
            # ТЗ 6.1: fills / partials recorded at the broker-confirmed leg level.
            if status == "filled":
                _metrics_record("orders_filled")
            elif status == "partially_filled":
                _metrics_record("orders_partial")
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
            if filled > 0.0:
                leg_entry = volume_ledger["legs"][str(leg)]
                leg_entry["filled_volume"] = round(filled, 8)
                leg_entry["remaining_volume"] = round(filled, 8)
        # netting: the aggregate position's actual fill IS the group's filled
        # volume (broker source of truth, P1.5.1 §12)
        if driver.account_mode == "netting":
            aggregate_filled = float(volume_ledger["legs"]["1"]["filled_volume"] or 0.0)
            volume_ledger["total_filled"] = round(aggregate_filled, 8)
        else:
            volume_ledger["total_filled"] = round(
                sum(float(entry["filled_volume"]) for entry in volume_ledger["legs"].values()), 8)
        volume_ledger["total_remaining"] = round(
            volume_ledger["total_filled"] - volume_ledger["total_closed"], 8)

        update_group_state(self.db_path, group_id, GroupState.SUBMITTED,
                           legs=legs, broker_ids=broker_ids,
                           be_state=current.get("be_state"),
                           volume=volume_ledger)
        save_group(self.db_path, spec, state=GroupState.SUBMITTED,
                   legs=legs, broker_ids=broker_ids, submitted=True,
                   be_state=current.get("be_state"),
                   intent_json=intent.model_dump_json(),
                   account_mode=driver.account_mode,
                   volume=volume_ledger)
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
        # ТЗ 6.1: rejections recorded with the reason code where they are
        # already logged (group_rejected ledger event) — one hook, no logic change.
        _metrics_record(f"rejected:{reason_code}", group_id=spec.group_id)
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
        # P1.6 §26/§27: actor (кто записал) и source (откуда факт) разделены.
        # Leg facts от broker order/deal несут source=mt5 + broker ids.
        deal_id = result.get("deal_id")
        order_id = result.get("order_id")
        append_trading_event(
            self.ledger_db_path, event_type=event_type,
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason=str(result.get("comment", "") or "") or None,
            group_id=spec.group_id, leg_id=new_leg_id(spec.group_id, leg),
            source="mt5" if (deal_id or order_id) else "simulator",
            source_type="deal" if deal_id else ("order" if order_id else "paper_driver"),
            source_id=f"DEAL-{deal_id}" if deal_id
            else (f"ORDER-{order_id}" if order_id else None),
            payload={
                "broker_order_id": order_id,
                "broker_position_id": result.get("position_id"),
                "broker_deal_id": deal_id,
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
        started = time.perf_counter()
        for group in list_groups(self.db_path):
            spec = group["spec"]
            if spec.mode != "demo":
                continue
            if group["state"] in (GroupState.RECONCILED, GroupState.STOPPED,
                                  GroupState.REJECTED, GroupState.EXPIRED,
                                  GroupState.CANCELLED, GroupState.FAILED):
                continue
            events.extend(self._advance_group(group))
        # ТЗ 6.1: poll duration for latency monitoring (poll_duration_ms).
        _metrics_record("poll_completed")
        _metrics_timing("poll_once", (time.perf_counter() - started) * 1000.0)
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
                # P1.5.1 §2–§9: a partial open NEVER fails the group while
                # already-opened legs remain at the broker. Compensation closes
                # them first; FAILED is reachable only after open risk == 0.
                result = self._begin_compensation(group, rejected)
                if result == "compensation_failed":
                    events.append("compensation_failed")
                    events.append("failed_with_open_risk")
                else:
                    events.append("partial_submission")
                    events.append("compensation_requested")
                return events
            filled = [item for item in group.get("legs", [])
                      if item.get("state") in ("SUBMITTED", "PARTIALLY_FILLED", "VIRTUAL")]
            if filled:
                self._open_group(group, inspection)
                events.append("group_opened")
                state = GroupState.OPENED

        # --- P1.5.1 compensation flow (§2/§8/§19) ----------------------------
        if state in (GroupState.PARTIAL_SUBMISSION, GroupState.COMPENSATION_REQUESTED,
                     GroupState.FAILED_WITH_OPEN_RISK):
            if state == GroupState.PARTIAL_SUBMISSION:
                # crash-safe retry: re-running _begin_compensation is idempotent
                # (mark_action guards every COMPENSATE action)
                rejected_legs = [item for item in group.get("legs", [])
                                 if item.get("state") == "REJECTED"]
                result = self._begin_compensation(group, rejected_legs)
                if result == "compensation_failed":
                    events.append("compensation_failed")
                    events.append("failed_with_open_risk")
                else:
                    events.append("compensation_requested")
                return events
            result = self._verify_compensation(group)
            if result == "confirmed":
                events.append("compensation_confirmed")
                events.append("group_failed_after_compensation")
                state = GroupState.FAILED
            elif result == "pending":
                events.append("compensation_pending")
            else:  # failed / failed_with_open_risk
                events.append("failed_with_open_risk")
                state = GroupState.FAILED_WITH_OPEN_RISK
            return events

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
                if self._netting_close_leg(group, 1, "CLOSE-TP1"):
                    events.append("tp1_filled")
                    state = GroupState.TP1_FILLED
            elif state == GroupState.BE_CONFIRMED and \
                    direction * (price - spec.geometry.tp2) >= 0.0:
                if self._netting_close_leg(group, 2, "CLOSE-TP2"):
                    events.append("tp2_filled")
                    state = GroupState.TP2_FILLED
            elif state == GroupState.TP2_FILLED and \
                    direction * (price - spec.geometry.tp3) >= 0.0:
                if self._netting_close_leg(group, 3, "CLOSE-TP3"):
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
    # Graceful shutdown (ТЗ 6.4 / P2-6)
    # ------------------------------------------------------------------

    def shutdown(self) -> dict[str, Any]:
        """Idempotent graceful shutdown: final poll_once + state persist +
        (optional) notifier warning.

        Safe to call multiple times: after the first call only a summary of
        the first shutdown is returned (``already_shutdown: True``). The
        final poll reuses poll_once(), whose per-group transitions already
        persist every state change, so no extra write path is introduced.
        """
        if getattr(self, "_shutdown_done", False):
            return {"already_shutdown": True,
                    "events": list(getattr(self, "_shutdown_events", []))}
        self._shutdown_done = True
        summary: dict[str, Any] = {"already_shutdown": False, "events": [],
                                   "final_poll_ok": False}
        logger.info("Graceful shutdown: running final poll")
        try:
            events = self.poll_once()
            summary["events"] = events
            summary["final_poll_ok"] = True
            logger.info("Final poll completed: %d events", len(events))
        except Exception as exc:  # noqa: BLE001 — shutdown must complete
            logger.error("Final poll failed: %s", exc)
            summary["final_poll_error"] = str(exc)
        # State persistence: trade-group state is already persisted per
        # transition inside poll_once; a final ledger marker keeps the log
        # auditable (no spec mutation).
        try:
            append_trading_event(
                self.ledger_db_path, event_type="system_shutdown",
                signal_id="-", asset_key="-", strategy_version="-",
                config_hash="-", actor="mt5_trade_group_executor",
                payload={"final_poll_ok": summary["final_poll_ok"],
                         "events": summary["events"]},
            )
            summary["state_persisted"] = True
        except Exception as exc:  # noqa: BLE001
            logger.error("Shutdown state persist failed: %s", exc)
            summary["state_persisted"] = False
        if self.notifier:
            try:
                self.notifier(
                    "⚠️ SYSTEM SHUTDOWN — positions may be unmanaged until restart"
                )
                summary["notified"] = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Shutdown notification failed: %s", exc)
                summary["notified"] = False
        self._shutdown_events = summary["events"]
        logger.info("Graceful shutdown complete")
        return summary

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _open_group(self, group: dict[str, Any], inspection) -> None:
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        require_transition(group["state"], GroupState.OPENED)
        # P0-4: actual fill = volume-weighted average price across every open
        # position of the group, not "whatever position was seen last". A
        # group opened as several partial fills would otherwise anchor
        # break-even to a single (possibly extreme) entry price.
        notional = 0.0
        volume_total = 0.0
        for pos in inspection.positions:
            price = float(pos.get("price_open") or 0.0)
            vol = float(pos.get("volume") or 0.0)
            if price <= 0.0 or vol <= 0.0:
                continue
            notional += price * vol
            volume_total += vol
        fill_price = (notional / volume_total) if volume_total > 0.0 else None
        legs = group.get("legs", [])
        driver = self._resolve_driver(spec)
        volume = dict(group.get("volume") or {})
        for item in legs:
            if item.get("state") == "VIRTUAL":
                continue  # netting virtual legs stay virtual (P1.5.1 §17)
            if item.get("state") == "PARTIALLY_FILLED":
                # hedging: manage the leg by its ACTUAL filled volume (§18)
                filled = float(item.get("broker", {}).get("filled_volume") or 0.0)
                item["filled_volume"] = round(filled, 8)
                item["remaining_volume"] = round(
                    filled - float(item.get("closed_volume") or 0.0), 8)
                continue
            if item.get("state") == "SUBMITTED":
                item["state"] = "OPEN"
                filled = float(item.get("broker", {}).get("filled_volume") or 0.0)
                if filled > 0.0:
                    item["filled_volume"] = round(filled, 8)
                    item["remaining_volume"] = round(
                        filled - float(item.get("closed_volume") or 0.0), 8)
        # P1.5.1 §12: for netting the broker's ACTUAL aggregate volume is the
        # source of truth for every later close computation.
        if driver.account_mode == "netting":
            pos = driver.query_position(new_leg_id(group_id, 1))
            if pos is not None:
                volume["total_filled"] = round(float(pos["volume"]), 8)
                volume["total_remaining"] = round(
                    float(volume.get("total_filled") or 0.0)
                    - float(volume.get("total_closed") or 0.0), 8)
        if fill_price is not None and spec.entry.actual_fill is None:
            # P0-4: log noticeable slippage between reference and executed VWAP
            # (accepted within the hard deviation gate, but worth surfacing).
            drift = abs(spec.entry.reference - fill_price) / spec.entry.reference
            if drift > FILL_DRIFT_WARN_RATIO:
                logger.warning(
                    "group %s fill drifted %.4f%% from reference: "
                    "reference=%.6g vwap=%.6g volume=%.6g",
                    group_id, drift * 100.0, spec.entry.reference,
                    fill_price, volume_total,
                )
            spec = spec.with_actual_fill(fill_price)
        update_group_state(self.db_path, group_id, GroupState.OPENED, legs=legs,
                           volume=volume)
        save_group(self.db_path, spec, state=GroupState.OPENED, legs=legs,
                   broker_ids=group.get("broker_ids", {}), submitted=True,
                   be_state=group.get("be_state"),
                   intent_json=group.get("intent_json"),
                   account_mode=group.get("account_mode"),
                   volume=volume)
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
            source="mt5" if broker_closed else "simulator",
            source_type="deal" if broker_closed else "paper_driver",
            source_id=f"DEAL-{int(fill_price * 1000)}" if broker_closed else None,
            payload={"fill_price": fill_price, "entry_actual_fill": spec.entry.actual_fill,
                     "mode": spec.mode,
                     "evidence": "broker_deal" if broker_closed else "partial_close_result"},
        )
        if self.notifier:
            self.notifier(self._tp1_message(spec))

    def _netting_close_leg(self, group: dict[str, Any], leg: int,
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
        if has_action(self.db_path, group_id, action_id):
            return False
        close_volume = self._netting_close_volume(group, leg)
        if close_volume <= 0.0:
            # increment below the broker volume_step/min: nothing fillable.
            # Note it ONCE (actionId-guarded) and keep the position managed.
            if mark_action(self.db_path, group_id, f"{action_id}-SKIP",
                           {"reason": "below_volume_step", "leg": leg}):
                emit_execution_error(
                    self.ledger_db_path, spec,
                    reason=f"{action_id} below volume_step (no fillable close)",
                    payload={"action_id": action_id, "leg": leg, "mode": spec.mode},
                    leg=leg,
                )
            return False
        driver = self._resolve_driver(spec)
        ref = new_leg_id(group_id, 1)  # aggregate position ref
        result = driver.close_partial(ref, close_volume)
        if result.get("status") != "filled":
            emit_execution_error(self.ledger_db_path, spec,
                                 reason=f"{action_id} rejected",
                                 payload={"action_id": action_id,
                                          "retcode": result.get("retcode"),
                                          "comment": result.get("comment"),
                                          "mode": spec.mode},
                                 leg=leg)
            return False
        filled_close = float(result.get("filled_volume") or close_volume)
        mark_action(self.db_path, group_id, action_id,
                    {"leg": leg, "volume": close_volume,
                     "filled_volume": filled_close,
                     "fill_price": result.get("fill_price")})
        self._update_volume_after_close(group, leg, filled_close)
        fill_price = float(result.get("fill_price") or 0.0)
        if leg == 1:
            self._tp1_filled(group, fill_price, broker_closed=False)
        elif leg == 2:
            self._tp2_filled(group, fill_price, broker_closed=False)
        else:
            self._tp3_filled(group, fill_price, broker_closed=False)
        return True

    def _floor_to_step(self, value: float, step: float) -> float:
        # P0-6: delegate to the shared Decimal(str(x)) ROUND_DOWN helper —
        # float division produced dust like 0.009999999999999998/0.01 -> 0.
        return floor_to_step(value, step)

    def _netting_close_volume(self, group: dict[str, Any], leg: int) -> float:
        """P1.5.1 §12–§15: deterministic close volume from the broker's ACTUAL
        position volume and the cumulative-allocation volume ledger."""
        group_id = group["group_id"]
        spec: TradeGroupSpec = group["spec"]
        driver = self._resolve_driver(spec)
        try:
            snapshot = MT5BrokerContext(self.mt5, magic=self.magic) \
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
            return self._floor_to_step(remaining_before, step)
        volume = group.get("volume") or {}
        total_filled = float(volume.get("total_filled") or 0.0)
        already_closed = float(volume.get("total_closed") or 0.0)
        cumulative = sum(float(t.allocation) for t in spec.targets if t.leg <= leg)
        desired_cumulative = total_filled * cumulative
        desired_increment = desired_cumulative - already_closed
        close_volume = min(desired_increment, remaining_before)
        # P0-6: the ledger difference above carries float dust
        # (0.03 * 1/3 == 0.009999999999999998); a step*1e-9 epsilon preserves
        # the intended lot count while _floor_to_step itself stays pure
        # ROUND_DOWN (P0-6 dust test relies on the pure behaviour).
        close_volume = self._floor_to_step(close_volume + step * 1e-9, step)
        if close_volume > 0.0 and close_volume < volume_min - 1e-9:
            # partial close below broker volume_min is unfillable (a FULL close
            # of the remaining volume is handled by the leg==3 branch)
            return 0.0
        return close_volume

    def _update_volume_after_close(self, group: dict[str, Any], leg: int,
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
        current = load_group(self.db_path, group_id)
        update_group_state(self.db_path, group_id, current["state"], volume=volume)

    # ------------------------------------------------------------------
    # P1.5.1 compensation flow (§2–§9): partial open -> close opened legs
    # ------------------------------------------------------------------

    def _begin_compensation(self, group: dict[str, Any],
                            rejected: list[dict[str, Any]]) -> str:
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
        opened = [item for item in group.get("legs", [])
                  if item.get("state") in ("SUBMITTED", "PARTIALLY_FILLED", "VIRTUAL")]
        update_group_state(self.db_path, group_id, GroupState.PARTIAL_SUBMISSION)
        append_trading_event(
            self.ledger_db_path, event_type="partial_submission",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=group_id,
            payload={
                "opened_legs": [item["leg"] for item in opened],
                "rejected_legs": [item["leg"] for item in rejected],
                "mode": spec.mode,
            },
        )
        if self.notifier:
            self.notifier(self._partial_submission_message(
                spec, [item["leg"] for item in opened],
                [item["leg"] for item in rejected]))

        driver = self._resolve_driver(spec)
        comp_state = {
            "status": "REQUESTED", "retries": int(group.get("comp_state", {}).get("retries", 0)),
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
            if action_id is not None and has_action(self.db_path, group_id, action_id):
                continue  # already successfully compensated (restart safety)
            pos = driver.query_position(ref)
            if pos is None:
                # already closed (or never opened): the goal is achieved
                if action_id is not None:
                    mark_action(self.db_path, group_id, action_id,
                                {"reason": "already_closed"})
                    comp_state["legs"][action_id] = {"status": "filled",
                                                     "comment": "already_closed"}
                continue
            result = driver.close_position(ref, float(pos["volume"]))
            label = action_id if action_id is not None else "GROUP"
            if result.get("status") == "filled":
                if action_id is not None:
                    mark_action(self.db_path, group_id, action_id,
                                {"leg": label, "volume": pos["volume"],
                                 "fill_price": result.get("fill_price")})
                comp_state["legs"][label] = result
                # volume ledger: closed volume grows with the ACTUAL closed
                # volume of every compensated reference (§10/§11/§25)
                filled_close = float(result.get("filled_volume") or pos["volume"])
                closed_total = round(closed_total + filled_close, 8)
                if label.startswith("COMPENSATE-L"):
                    leg_no = int(label.rsplit("L", 1)[1])
                    legs_ledger = volume.setdefault("legs", {})
                    entry = legs_ledger.setdefault(str(leg_no), {})
                    entry["closed_volume"] = round(
                        float(entry.get("closed_volume") or 0.0) + filled_close, 8)
                    entry["remaining_volume"] = 0.0
            else:
                comp_state["legs"][label] = result  # rejected; UNMARKED -> retried
                failures.append(label)
        volume["total_closed"] = round(closed_total, 8)
        volume["total_remaining"] = round(
            float(volume.get("total_filled") or 0.0) - closed_total, 8)

        if failures:
            comp_state["status"] = "FAILED"
            update_group_state(self.db_path, group_id,
                               GroupState.FAILED_WITH_OPEN_RISK,
                               comp_state=comp_state)
            self._record_compensation_failure(spec, comp_state, failures,
                                              reason="compensation close rejected")
            return "compensation_failed"
        update_group_state(self.db_path, group_id, GroupState.COMPENSATION_REQUESTED,
                           comp_state=comp_state, volume=volume)
        append_trading_event(
            self.ledger_db_path, event_type="compensation_requested",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=group_id,
            payload={"action_ids": list(comp_state["legs"].keys()),
                     "rejected_legs": [item["leg"] for item in rejected],
                     "mode": spec.mode},
        )
        return "compensation_requested"

    def _verify_compensation(self, group: dict[str, Any]) -> str:
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
        group = load_group(self.db_path, group_id)
        spec: TradeGroupSpec = group["spec"]
        driver = self._resolve_driver(spec)
        comp_state = dict(group.get("comp_state") or {})
        comp_legs = dict(comp_state.get("legs", {}) or {})
        retries = int(comp_state.get("retries", 0))

        # --- Phase 1: re-send rejected (unmarked) closes ---------------------
        rejected_refs = [ref for ref, result in comp_legs.items()
                         if result.get("status") != "filled"]
        if rejected_refs:
            if retries >= self.max_compensation_retries:
                comp_state["status"] = "FAILED"
                if not comp_state.get("recorded"):
                    comp_state["recorded"] = True
                    self._record_compensation_failure(
                        spec, comp_state, rejected_refs,
                        reason="compensation retries exhausted")
                update_group_state(self.db_path, group_id,
                                   GroupState.FAILED_WITH_OPEN_RISK,
                                   comp_state=comp_state)
                return "failed"
            retries += 1
            comp_state["retries"] = retries
            still_failed: list[str] = []
            for ref in rejected_refs:
                ticket = self._compensation_ticket(group, driver, ref)
                pos = driver.query_position(ticket) if ticket else None
                if pos is None:
                    # already closed between polls -> goal achieved
                    comp_legs[ref] = {"status": "filled", "comment": "already_closed"}
                    mark_action(self.db_path, group_id,
                                self._compensation_action_id(ref),
                                {"reason": "already_closed_on_retry"})
                    continue
                result = driver.close_position(ticket, float(pos["volume"]))
                comp_legs[ref] = result
                if result.get("status") == "filled":
                    mark_action(self.db_path, group_id,
                                self._compensation_action_id(ref),
                                {"leg": ref, "volume": pos["volume"],
                                 "fill_price": result.get("fill_price")})
                else:
                    still_failed.append(ref)
            comp_state["legs"] = comp_legs
            if still_failed:
                comp_state["status"] = "FAILED"
                if not comp_state.get("recorded"):
                    comp_state["recorded"] = True
                    self._record_compensation_failure(
                        spec, comp_state, still_failed,
                        reason="compensation close rejected on retry")
                update_group_state(self.db_path, group_id,
                                   GroupState.FAILED_WITH_OPEN_RISK,
                                   comp_state=comp_state)
                return "failed"
            comp_state.pop("recorded", None)
            update_group_state(self.db_path, group_id,
                               GroupState.COMPENSATION_REQUESTED,
                               comp_state=comp_state)

        # --- Phase 2: verify every marked reference is closed ----------------
        pending: list[str] = []
        for ref in comp_legs:
            ticket = self._compensation_ticket(group, driver, ref)
            pos = driver.query_position(ticket) if ticket else None
            if pos is not None:
                pending.append(ref)
        if pending:
            comp_state["retries"] = int(comp_state.get("retries", 0)) + 1
            comp_state["pending"] = pending
            if comp_state["retries"] >= self.max_compensation_retries:
                comp_state["status"] = "FAILED"
                if not comp_state.get("recorded"):
                    comp_state["recorded"] = True
                    self._record_compensation_failure(
                        spec, comp_state, pending,
                        reason="compensation not confirmed within retry budget")
                update_group_state(self.db_path, group_id,
                                   GroupState.FAILED_WITH_OPEN_RISK,
                                   comp_state=comp_state)
                return "failed"
            comp_state["status"] = "REQUESTED"
            update_group_state(self.db_path, group_id,
                               GroupState.COMPENSATION_REQUESTED,
                               comp_state=comp_state)
            return "pending"

        # every compensated reference is closed -> consume their OUT deals so
        # they are never re-classified as TP/SL (§19) and finalize the group
        self._consume_compensation_deals(group, driver, comp_legs)
        volume = dict(group.get("volume") or {})
        volume["total_closed"] = round(float(volume.get("total_filled") or 0.0), 8)
        volume["total_remaining"] = 0.0
        for entry in volume.get("legs", {}).values():
            entry["remaining_volume"] = 0.0
        comp_state["status"] = "CONFIRMED"
        # confirmed from either COMPENSATION_REQUESTED or a retried
        # FAILED_WITH_OPEN_RISK (both transitions are allowed)
        require_transition(group["state"], GroupState.COMPENSATION_CONFIRMED)
        update_group_state(self.db_path, group_id, GroupState.COMPENSATION_CONFIRMED,
                           comp_state=comp_state, volume=volume)
        append_trading_event(
            self.ledger_db_path, event_type="compensation_confirmed",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            group_id=group_id,
            payload={"open_risk": 0.0, "mode": spec.mode},
        )
        require_transition(GroupState.COMPENSATION_CONFIRMED, GroupState.FAILED)
        update_group_state(self.db_path, group_id, GroupState.FAILED,
                           comp_state=comp_state, volume=volume)
        if self.notifier:
            reason = ", ".join(
                f"LEG{leg}_REJECTED" for leg in sorted(
                    int(i["leg"]) for i in group.get("legs", [])
                    if i.get("state") == "REJECTED"))
            self.notifier(self._failed_after_compensation_message(spec, reason))
        return "confirmed"

    def _compensation_action_id(self, ref: str) -> str:
        """COMPENSATE actionId for a comp_state leg key (keys ARE action ids)."""
        return ref

    def _compensation_ticket(self, group: dict[str, Any], driver,
                             ref: str) -> str:
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

    def _consume_compensation_deals(self, group: dict[str, Any], driver,
                                    comp_legs: dict[str, Any]) -> None:
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
                    mark_action(self.db_path, group_id, f"DEAL-{deal.get('ticket')}",
                                {"kind": "compensation"})

    def _record_compensation_failure(self, spec: TradeGroupSpec,
                                     comp_state: dict[str, Any],
                                     open_refs: list[Any],
                                     reason: str) -> None:
        """FAILED_WITH_OPEN_RISK: explicit ledger facts + emergency Telegram.
        Reconciliation keeps polling this non-terminal state (P1.5.1 §8)."""
        append_trading_event(
            self.ledger_db_path, event_type="compensation_failed",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason=reason, group_id=spec.group_id,
            payload={"open_refs": [str(r) for r in open_refs],
                     "retries": int(comp_state.get("retries", 0)),
                     "mode": spec.mode},
        )
        append_trading_event(
            self.ledger_db_path, event_type="failed_with_open_risk",
            signal_id=spec.signal_id, asset_key=spec.asset_key,
            strategy_version=spec.strategy_version, config_hash=spec.config_hash,
            model_hash=spec.model_hash, actor="mt5_trade_group_executor",
            reason=reason, group_id=spec.group_id,
            payload={"open_refs": [str(r) for r in open_refs],
                     "mode": spec.mode},
        )
        if self.notifier:
            self.notifier(self._open_risk_message(spec, open_refs))

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
            source="mt5" if broker_closed else "simulator",
            source_type="deal" if broker_closed else "paper_driver",
            source_id=f"DEAL-{int(fill_price * 1000)}" if broker_closed else None,
            payload={"fill_price": fill_price, "mode": spec.mode,
                     "tp2": spec.geometry.tp2,
                     "evidence": "broker_deal" if broker_closed else "partial_close_result"},
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
            source="mt5" if broker_closed else "simulator",
            source_type="deal" if broker_closed else "paper_driver",
            source_id=f"DEAL-{int(fill_price * 1000)}" if broker_closed else None,
            payload={"fill_price": fill_price, "mode": spec.mode,
                     "tp3": spec.geometry.tp3,
                     "evidence": "broker_deal" if broker_closed else "partial_close_result"},
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
            source="mt5", source_type="deal",
            source_id=f"DEAL-{int(stop_price * 1000)}",
            payload={"stop_price": stop_price, "mode": spec.mode,
                     "sl": spec.geometry.sl,
                     "evidence": "broker_deal"},
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
            source="mt5", source_type="position",
            source_id=f"POSITION:{spec.group_id}:{int(requested * 1000)}",
            payload={"confirmed_price": requested, "raw_price": be_state.get("raw_price"),
                     "mode": spec.mode,
                     "evidence": "broker_position_query"},
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
    # Telegram messages (ТЗ §35/§36) — only broker-confirmed events.
    # P2-4: bodies live in execution.telegram_formatter; these thin delegates
    # remain for backward compatibility (tests may patch/mock them).
    # ------------------------------------------------------------------

    def _opened_message(self, spec: TradeGroupSpec) -> str:
        return tf.format_group_opened(spec)

    def _tp1_message(self, spec: TradeGroupSpec) -> str:
        return tf.format_tp1_filled(spec)

    def _tp_message(self, spec: TradeGroupSpec, label: str, header: str) -> str:
        return tf.format_tp_filled(spec, label, header)

    def _be_message(self, spec: TradeGroupSpec, sl_price: float) -> str:
        return tf.format_be_confirmed(spec, sl_price)

    def _stopped_message(self, spec: TradeGroupSpec) -> str:
        return tf.format_stopped(spec)

    def _partial_submission_message(self, spec: TradeGroupSpec,
                                    opened_legs: list[int],
                                    rejected_legs: list[int]) -> str:
        return tf.format_partial_submission(spec, opened_legs, rejected_legs)

    def _failed_after_compensation_message(self, spec: TradeGroupSpec,
                                           reason: str) -> str:
        return tf.format_failed_after_compensation(spec, reason)

    def _open_risk_message(self, spec: TradeGroupSpec, open_refs: list[Any]) -> str:
        return tf.format_open_risk(spec, open_refs)
