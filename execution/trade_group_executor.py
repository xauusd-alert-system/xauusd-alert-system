"""
TradeGroupExecutor — lifecycle engine for TradeGroupSpec v1 (paper-only, ТЗ §29).

Rollout gate (fail-closed): the executor runs groups ONLY in ``mode=paper``.
``mode=demo`` requires an explicit ``allow_demo=True`` (env
``TRADE_GROUP_ENABLE_DEMO=1``); ``mode=live`` is always blocked with
``LiveExecutionForbidden``. The current live path in ``execution/mt5_trader.py``
is untouched — this module is the isolated paper/demo harness.

Semantics guaranteed here (ТЗ §16/§18/§25/§28):

* TP1 is confirmed ONLY from a broker execution event (paper driver simulates
  fills from price ticks and labels them ``simulated`` in the ledger).
* BE uses the ACTUAL FILL, never the signal reference (ТЗ §6/§17).
* BE_CONFIRMED is emitted only after the driver's SL modify is accepted AND the
  subsequent broker query returns the requested SL; otherwise BE_RETRY (bounded),
  never BE_CONFIRMED.
* ``try_mark_submitted`` guards restarts: a recovered executor can never
  duplicate a submission (ТЗ §25/§28.8).
* Every lifecycle transition appends a hash-chained ledger event with groupId,
  legId, mode, source, requested vs actual values and reason.
"""
from __future__ import annotations

import os
from typing import Any, Protocol

from data.trade_group_store import (
    load_group,
    save_group,
    try_mark_submitted,
    update_group_state,
)
from data.trading_event_ledger import append_trading_event
from execution.trade_geometry import BrokerSnapshot, CostSnapshot, compute_break_even
from execution.trade_group import (
    BeStatus,
    GroupState,
    TradeGroupSpec,
    new_leg_id,
    require_transition,
)


class LiveExecutionForbidden(RuntimeError):
    """mode=live is never allowed by the executor (ТЗ §29/§33)."""


class DemoExecutionNotEnabled(RuntimeError):
    """mode=demo requires TRADE_GROUP_ENABLE_DEMO=1 (explicit gate)."""


class DuplicateSubmissionError(RuntimeError):
    """Group was already submitted; restart recovery must not resubmit."""


class GroupStateError(RuntimeError):
    """Invalid lifecycle transition attempted."""


# --------------------------------------------------------------------------
# Driver protocol (broker-facing abstraction; hedging vs netting, ТЗ §13)
# --------------------------------------------------------------------------

class GroupDriver(Protocol):
    """Minimal broker surface needed by the executor."""

    mode: str                       # "paper" | "demo" | "live"
    account_mode: str               # "hedging" | "netting"

    def submit_leg(self, spec: TradeGroupSpec, leg: int, volume: float) -> dict[str, Any]:
        """Submit one leg; returns broker ids {order_id, position_id}."""

    def modify_sl(self, reference: str, sl: float) -> tuple[bool, str]:
        """Request SL modification; returns (accepted, comment)."""

    def query_sl(self, reference: str) -> float | None:
        """Broker query of the current SL; None when the ref is unknown."""


class PaperDriver:
    """Deterministic in-memory simulated broker driver (mode=paper).

    ``account_mode="netting"`` (default): leg 1 creates the single aggregate
    position; legs 2/3 are VIRTUAL (ТЗ §13.2 — UI must say "3-leg trade group",
    never "3 independent positions"). ``account_mode="hedging"``: each leg gets
    its own position id (ТЗ §13.1).
    """

    def __init__(self, account_mode: str = "netting"):
        if account_mode not in {"hedging", "netting"}:
            raise ValueError(f"unsupported account mode {account_mode!r}")
        self.mode = "paper"
        self.account_mode = account_mode
        self.positions: dict[str, dict[str, Any]] = {}   # ref -> {volume, sl, tp}
        self.order_seq = 0
        self.reject_modify = False                        # injectable failure

    def submit_leg(self, spec: TradeGroupSpec, leg: int, volume: float) -> dict[str, Any]:
        ref = new_leg_id(spec.group_id, leg)
        self.order_seq += 1
        if self.account_mode == "netting" and leg > 1:
            # virtual leg: no physical position, shares the aggregate position
            aggregate = self.positions.get(new_leg_id(spec.group_id, 1), {})
            position_id = aggregate.get("position_id", f"PG-{spec.group_id}-POS")
        else:
            position_id = f"PG-{spec.group_id}-L{leg}"
            self.positions[ref] = {
                "position_id": position_id,
                "volume": volume,
                "sl": spec.geometry.sl,
                "tp": spec.leg_price(leg),
            }
        return {"order_id": f"PO-{spec.group_id}-{leg}-{self.order_seq}",
                "position_id": position_id}

    def _resolve_ref(self, reference: str) -> str:
        """Netting: virtual legs (2/3) share the aggregate position's SL, so a
        modify/query on a virtual leg routes to the physical leg-1 position."""
        if self.account_mode == "netting" and reference not in self.positions:
            group_id = reference.rsplit("-L", 1)[0]
            physical = new_leg_id(group_id, 1)
            if physical in self.positions:
                return physical
        return reference

    def modify_sl(self, reference: str, sl: float) -> tuple[bool, str]:
        if self.reject_modify:
            return False, "simulated modify rejection (freeze/requote)"
        ref = self._resolve_ref(reference)
        if ref not in self.positions:
            return False, f"unknown position ref {reference}"
        self.positions[ref]["sl"] = sl
        return True, "done"

    def query_sl(self, reference: str) -> float | None:
        ref = self._resolve_ref(reference)
        pos = self.positions.get(ref)
        if pos is None:
            return None
        return float(pos["sl"])


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------

class TradeGroupExecutor:
    def __init__(
        self,
        db_path: str,
        *,
        ledger_db_path: str | None = None,
        driver: GroupDriver | None = None,
        allow_demo: bool | None = None,
        max_be_retries: int = 3,
        cost: CostSnapshot | None = None,
        broker: BrokerSnapshot | None = None,
    ):
        self.db_path = db_path
        self.ledger_db_path = ledger_db_path or db_path
        self.driver = driver or PaperDriver()
        # Fail-closed gate: demo execution is enabled ONLY by an explicit
        # allow_demo=True argument or the TRADE_GROUP_ENABLE_DEMO=1 env var.
        # Unset env / False -> demo blocked; live always blocked.
        if allow_demo is None:
            allow_demo = os.environ.get("TRADE_GROUP_ENABLE_DEMO", "0") == "1"
        self.allow_demo = bool(allow_demo)
        self.max_be_retries = max(1, int(max_be_retries))
        self.cost = cost or CostSnapshot()
        self.broker = broker or BrokerSnapshot()

    # --- mode gate (ТЗ §29) -------------------------------------------------

    def _gate_mode(self, spec: TradeGroupSpec) -> None:
        if spec.mode == "live":
            raise LiveExecutionForbidden(
                "trade-group execution is paper/demo-only until separate "
                "explicit live approval (ТЗ §29/§32 P2 live promotion)"
            )
        if spec.mode == "demo" and not self.allow_demo:
            raise DemoExecutionNotEnabled(
                "demo trade-group execution requires TRADE_GROUP_ENABLE_DEMO=1"
            )

    # --- ledger helper ------------------------------------------------------

    def _append(self, spec: TradeGroupSpec, event_type: str, *,
                leg: int | None = None, reason: str | None = None,
                payload: dict[str, Any] | None = None,
                actor: str = "trade_group_executor"):
        append_trading_event(
            self.ledger_db_path,
            event_type=event_type,
            signal_id=spec.signal_id,
            asset_key=spec.asset_key,
            strategy_version=spec.strategy_version,
            config_hash=spec.config_hash,
            model_hash=spec.model_hash,
            actor=actor,
            reason=reason,
            group_id=spec.group_id,
            leg_id=new_leg_id(spec.group_id, leg) if leg is not None else None,
            payload=payload or {},
        )

    def _require_group(self, group_id: str) -> dict[str, Any]:
        current = load_group(self.db_path, group_id)
        if current is None:
            raise GroupStateError(f"group {group_id} not found")
        return current

    def _ensure_open(self, group_id: str) -> GroupState:
        """SUBMITTED -> OPENED on the first broker market event (ТЗ §23).

        The first confirmed broker event after submission implies the position
        is open; without this promotion the state machine could never leave
        SUBMITTED."""
        current = self._require_group(group_id)
        if current["state"] == GroupState.SUBMITTED:
            require_transition(GroupState.SUBMITTED, GroupState.OPENED)
            update_group_state(self.db_path, group_id, GroupState.OPENED)
            return GroupState.OPENED
        return current["state"]

    # --- lifecycle ----------------------------------------------------------

    def create_group(self, spec: TradeGroupSpec) -> GroupState:
        """Register a validated spec: DRAFT -> VALIDATED."""
        self._gate_mode(spec)
        require_transition(GroupState.DRAFT, GroupState.VALIDATED)
        save_group(self.db_path, spec, state=GroupState.DRAFT)
        save_group(self.db_path, spec, state=GroupState.VALIDATED)
        self._append(spec, "signal_validated",
                     payload={"geometry": spec.as_geometry_payload(),
                              "state": "VALIDATED", "mode": spec.mode})
        self._append(spec, "trade_intent_created",
                     payload={"intent_id": spec.intent_id, "group_id": spec.group_id,
                              "mode": spec.mode})
        return GroupState.VALIDATED

    def submit_group(self, group_id: str) -> GroupState:
        """VALIDATED -> SUBMITTED; guarded against duplicate submission."""
        current = self._require_group(group_id)
        spec = current["spec"]
        if not try_mark_submitted(self.db_path, group_id):
            raise DuplicateSubmissionError(
                f"group {group_id} was already submitted; "
                f"restart recovery must not resubmit (ТЗ §25)"
            )
        require_transition(current["state"], GroupState.SUBMITTED)
        legs = []
        broker_ids = {}
        for leg in (1, 2, 3):
            target = next(t for t in spec.targets if t.leg == leg)
            volume = round(spec.risk.total_volume * target.allocation, 8)
            leg_state = "SKIPPED" if volume <= 0 else "SUBMITTED"
            result = self.driver.submit_leg(spec, leg, volume)
            broker_ids[new_leg_id(group_id, leg)] = result
            legs.append({
                "leg": leg, "price": spec.leg_price(leg),
                "volume": volume, "state": leg_state,
                "broker": result,
            })
            if leg_state == "SUBMITTED":
                self._append(spec, "leg_submitted", leg=leg,
                             payload={"volume": volume, "price": spec.leg_price(leg),
                                      "broker_ids": result, "mode": spec.mode})
            else:
                self._append(spec, "leg_rejected", leg=leg, reason="SKIPPED_ZERO_VOLUME",
                             payload={"volume": volume, "mode": spec.mode})
        update_group_state(self.db_path, group_id, GroupState.SUBMITTED,
                           legs=legs, broker_ids=broker_ids)
        self._append(spec, "group_submitted",
                     payload={"account_mode": self.driver.account_mode,
                              "legs": legs, "geometry": spec.as_geometry_payload(),
                              "mode": spec.mode})
        return GroupState.SUBMITTED

    def on_leg_filled(self, group_id: str, leg: int, fill_price: float) -> GroupState:
        """Confirm a leg fill from a broker execution event (TP fill).

        Leg 1 fill -> TP1_FILLED (actual fill attached to the spec).
        Leg 2/3 fills -> TP2_FILLED / TP3_FILLED -> RECONCILED.
        """
        current = self._require_group(group_id)
        spec = current["spec"]
        if leg == 1:
            self._ensure_open(group_id)
            current = self._require_group(group_id)
            require_transition(current["state"], GroupState.TP1_FILLED)
            if spec.entry.actual_fill is None:
                spec = spec.with_actual_fill(fill_price)
            legs = current["legs"]
            for item in legs:
                if item["leg"] == 1:
                    item["state"] = "CLOSED"
                    item["fill_price"] = fill_price
            save_group(self.db_path, spec, state=GroupState.TP1_FILLED,
                       legs=legs, be_state=current["be_state"],
                       broker_ids=current["broker_ids"], submitted=True)
            self._append(spec, "tp1_filled", leg=1,
                         payload={"fill_price": fill_price,
                                  "entry_actual_fill": spec.entry.actual_fill,
                                  "mode": spec.mode})
            return GroupState.TP1_FILLED
        return self._on_tp_leg_filled(group_id, leg, fill_price)

    def _on_tp_leg_filled(self, group_id: str, leg: int, fill_price: float) -> GroupState:
        current = self._require_group(group_id)
        spec = current["spec"]
        if leg == 2:
            self._ensure_open(group_id)
            current = self._require_group(group_id)
            require_transition(current["state"], GroupState.TP2_FILLED)
            next_state, event = GroupState.TP2_FILLED, "tp2_filled"
        elif leg == 3:
            require_transition(current["state"], GroupState.TP3_FILLED)
            next_state, event = GroupState.TP3_FILLED, "tp3_filled"
        else:
            raise ValueError(f"unknown leg {leg}")
        legs = current["legs"]
        for item in legs:
            if item["leg"] == leg:
                item["state"] = "CLOSED"
                item["fill_price"] = fill_price
        update_group_state(self.db_path, group_id, next_state, legs=legs)
        self._append(spec, event, leg=leg, payload={"fill_price": fill_price,
                                                    "mode": spec.mode})
        if leg == 3:
            update_group_state(self.db_path, group_id, GroupState.RECONCILED, legs=legs)
            self._append(spec, "group_reconciled",
                         payload={"mode": spec.mode, "geometry": spec.as_geometry_payload()})
            return GroupState.RECONCILED
        return next_state

    def request_break_even(self, group_id: str) -> GroupState:
        """TP1_FILLED -> BE_REQUESTED. BE price is computed from the ACTUAL FILL
        (ТЗ §6/§17); without a confirmed fill this is an error."""
        current = self._require_group(group_id)
        spec = current["spec"]
        require_transition(current["state"], GroupState.BE_REQUESTED)
        if spec.entry.actual_fill is None:
            raise GroupStateError(
                "break-even requires a confirmed actual fill (entry.actual_fill)"
            )
        be = compute_break_even(
            side=spec.side, actual_fill=spec.entry.actual_fill,
            cost=self.cost, broker=self.broker,
        )
        be_state = dict(current["be_state"])
        be_state.update({
            "status": BeStatus.BE_REQUESTED.value,
            "raw_price": be["raw_price"],
            "protected_price": be["protected_price"],
            "requested_price": be["protected_price"],
            "retries": int(be_state.get("retries", 0)),
        })
        update_group_state(self.db_path, group_id, GroupState.BE_REQUESTED,
                           be_state=be_state)
        self._append(spec, "be_requested",
                     payload={"raw_price": be["raw_price"],
                              "protected_price": be["protected_price"],
                              "requested_price": be["protected_price"],
                              "apply_to": spec.break_even.apply_to,
                              "mode": spec.mode})
        return GroupState.BE_REQUESTED

    def confirm_break_even(self, group_id: str) -> GroupState:
        """BE_REQUESTED/BE_RETRY -> BE_CONFIRMED ONLY after the broker accepts
        the SL modify AND the broker query returns the requested SL (ТЗ §18)."""
        current = self._require_group(group_id)
        spec = current["spec"]
        state = current["state"]
        if state not in (GroupState.BE_REQUESTED, GroupState.BE_RETRY):
            raise GroupStateError(
                f"confirm_break_even requires BE_REQUESTED/BE_RETRY, got {state.value}"
            )
        be_state = dict(current["be_state"])
        requested = float(be_state.get("requested_price") or 0.0)
        if requested <= 0.0:
            raise GroupStateError("no requested BE price; call request_break_even first")

        # ТЗ §16: BE applies to EVERY remaining leg in break_even.apply_to
        # (netting: the driver resolves virtual legs to the aggregate position).
        refs = [new_leg_id(group_id, leg) for leg in spec.break_even.apply_to]
        for ref in refs:
            accepted, comment = self.driver.modify_sl(ref, requested)
            if not accepted:
                return self._be_retry_or_fail(group_id, spec, be_state, requested, comment)
        for ref in refs:
            observed = self.driver.query_sl(ref)
            if observed is None or abs(observed - requested) > 1e-9:
                return self._be_retry_or_fail(
                    group_id, spec, be_state, requested,
                    f"SL query mismatch: observed={observed}",
                )

        be_state.update({"status": BeStatus.BE_CONFIRMED.value,
                         "confirmed_price": requested, "last_error": None})
        update_group_state(self.db_path, group_id, GroupState.BE_CONFIRMED,
                           be_state=be_state)
        self._append(spec, "be_confirmed",
                     payload={"confirmed_price": requested,
                              "raw_price": be_state.get("raw_price"),
                              "mode": spec.mode})
        return GroupState.BE_CONFIRMED

    def _be_retry_or_fail(self, group_id: str, spec: TradeGroupSpec,
                          be_state: dict[str, Any], requested: float,
                          error: str) -> GroupState:
        """Bounded BE retry: BE_RETRY until max_be_retries, then FAILED.
        BE_CONFIRMED is NEVER emitted on this path (ТЗ §18/§28.7)."""
        retries = int(be_state.get("retries", 0)) + 1
        be_state.update({"status": BeStatus.BE_RETRY.value, "retries": retries,
                         "last_error": error})
        next_state = GroupState.BE_RETRY if retries < self.max_be_retries \
            else GroupState.FAILED
        update_group_state(self.db_path, group_id, next_state, be_state=be_state)
        self._append(spec, "be_retry" if next_state == GroupState.BE_RETRY
                     else "leg_rejected",
                     reason=error,
                     payload={"retries": retries, "requested_price": requested,
                              "mode": spec.mode})
        return next_state

    def on_stopped(self, group_id: str, stop_price: float) -> GroupState:
        """Broker stop event -> STOPPED (stop_filled)."""
        self._ensure_open(group_id)
        current = self._require_group(group_id)
        spec = current["spec"]
        require_transition(current["state"], GroupState.STOPPED)
        update_group_state(self.db_path, group_id, GroupState.STOPPED)
        self._append(spec, "stop_filled",
                     payload={"stop_price": stop_price, "mode": spec.mode})
        return GroupState.STOPPED

    # --- paper market simulation (mode=paper only) --------------------------

    def simulate_tick(self, group_id: str, price: float) -> list[str]:
        """Paper-only price feed: fills TP/SL legs when price crosses levels.

        Returns the list of events emitted. Every simulated fill is labeled
        ``simulated=true, mode=paper`` in the ledger — never a broker fact.
        """
        if self.driver.mode != "paper":
            raise RuntimeError("simulate_tick is paper-driver only")
        current = self._require_group(group_id)
        spec = current["spec"]
        state = current["state"]
        events: list[str] = []
        direction = 1.0 if spec.side == "long" else -1.0

        stop_hit = state in {GroupState.SUBMITTED, GroupState.OPENED,
                             GroupState.TP1_FILLED, GroupState.BE_REQUESTED,
                             GroupState.BE_RETRY, GroupState.BE_CONFIRMED,
                             GroupState.TP2_FILLED} and \
            (direction * (price - spec.geometry.sl) <= 0.0)
        if stop_hit:
            self.on_stopped(group_id, spec.geometry.sl)
            return ["stop_filled"]

        if state in {GroupState.SUBMITTED, GroupState.OPENED}:
            if direction * (price - spec.geometry.tp1) >= 0.0:
                self.on_leg_filled(group_id, 1, spec.geometry.tp1)
                events.append("tp1_filled")
        elif state == GroupState.BE_CONFIRMED:
            if direction * (price - spec.geometry.tp2) >= 0.0:
                self.on_leg_filled(group_id, 2, spec.geometry.tp2)
                events.append("tp2_filled")
        elif state == GroupState.TP2_FILLED:
            if direction * (price - spec.geometry.tp3) >= 0.0:
                self.on_leg_filled(group_id, 3, spec.geometry.tp3)
                events.append("tp3_filled")
        return events

    # --- restart recovery (ТЗ §25/§28.8) ------------------------------------

    def recover_after_restart(self, group_id: str) -> dict[str, Any]:
        """Load a group from the store and reconcile with the driver.

        Guarantees: no duplicate submission (``submitted`` flag is sticky), TP
        hit flags and BE state are preserved, and the group resumes from its
        persisted state.
        """
        current = self._require_group(group_id)
        spec = current["spec"]
        state = current["state"]
        if current["submitted"]:
            for item in current["legs"]:
                ref = new_leg_id(group_id, item["leg"])
                sl = self.driver.query_sl(ref)
                if item.get("broker", {}).get("position_id"):
                    item["verified_after_restart"] = True
                if sl is not None:
                    item["sl_after_restart"] = sl
            update_group_state(self.db_path, group_id, state, legs=current["legs"])
        self._append(spec, "group_reconciled", reason="restart_recovery",
                     payload={"state": state.value, "submitted": current["submitted"],
                              "mode": spec.mode})
        return current
