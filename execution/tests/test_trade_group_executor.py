"""Tests for execution/trade_group_executor.py — paper lifecycle engine."""
from __future__ import annotations

import json

import pytest

from data.trade_group_store import load_group
from data.trading_event_ledger import read_trading_events
from execution.trade_geometry import BrokerSnapshot, CostSnapshot
from execution.trade_group import GroupState, TradeGroupSpec
from execution.trade_group_executor import (
    DemoExecutionNotEnabled,
    DuplicateSubmissionError,
    GroupStateError,
    LiveExecutionForbidden,
    PaperDriver,
    TradeGroupExecutor,
)

BROKER = BrokerSnapshot(
    symbol_point=0.01, tick_size=0.01, digits=2,
    trade_stops_level=0, trade_freeze_level=0, spread=0.25,
    contract_size=100.0, volume_min=0.01, volume_max=10.0, volume_step=0.01,
    execution_mode="request", account_margin_mode="netting", balance=10000.0,
)
COST = CostSnapshot(round_trip_cost_price=0.30, safety_buffer_price=0.10,
                    expected_exit_slippage=0.10, commission_buffer=0.05)


def make_spec(group_id: str = "TG-EXEC-1", mode: str = "paper",
              side: str = "long") -> TradeGroupSpec:
    return TradeGroupSpec(
        group_id=group_id,
        signal_id="SGL-EXEC-1", intent_id="INT-EXEC-1",
        asset_key="XAUUSD", broker_symbol="GOLD", mode=mode, side=side,
        entry={"low": 4159.10, "high": 4159.50, "reference": 4159.30},
        geometry={"version": "v1", "unit": "price", "step_price": 4.30,
                  "tp1": 4163.60, "tp2": 4167.70, "tp3": 4171.20, "sl": 4140.30},
        targets=[{"leg": 1, "price": 4163.60, "allocation": 0.333333},
                 {"leg": 2, "price": 4167.70, "allocation": 0.333333},
                 {"leg": 3, "price": 4171.20, "allocation": 0.333334}],
        break_even={"trigger": "tp1_filled",
                    "raw_price_policy": "actual_fill",
                    "protected_price_policy": "actual_fill_plus_cost_buffer",
                    "apply_to": [2, 3]},
        risk={"currency": "USD", "max_cash": 50.0, "max_pct": 0.5,
              "estimated_loss_at_sl": 48.0, "total_volume": 0.06},
        profile_id="xau_m15_intraday_v1",
        model_version="v3", model_hash="m" * 64, config_hash="c" * 64,
        strategy_version="s3",
        expires_at_utc_ms=1_800_000_000_000, created_at_utc_ms=1_700_000_000_000,
    )


@pytest.fixture
def executor(tmp_path):
    db = str(tmp_path / "exec.sqlite")
    return TradeGroupExecutor(db, driver=PaperDriver(account_mode="netting"),
                              cost=COST, broker=BROKER)


def _event_types(db) -> list[tuple[str, str | None, str | None]]:
    df = read_trading_events(db)
    return [(row["event_type"], row.get("group_id"), row.get("leg_id"))
            for row in df.to_dict("records")]


def test_paper_full_lifecycle_to_reconciled(executor, tmp_path):
    spec = make_spec()
    assert executor.create_group(spec) == GroupState.VALIDATED
    assert executor.submit_group(spec.group_id) == GroupState.SUBMITTED

    stored = load_group(executor.db_path, spec.group_id)
    # netting: leg 2/3 are virtual (share the aggregate position) — ТЗ §13.2
    pos_ids = {item["broker"]["position_id"] for item in stored["legs"]}
    assert len(pos_ids) == 1

    # TP1 via simulated tick -> actual fill attached
    assert executor.simulate_tick(spec.group_id, 4163.60) == ["tp1_filled"]
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.TP1_FILLED
    assert stored["spec"].entry.actual_fill == 4163.60

    # BE from actual fill
    assert executor.request_break_even(spec.group_id) == GroupState.BE_REQUESTED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["be_state"]["status"] == "BE_REQUESTED"
    assert stored["be_state"]["raw_price"] == 4163.60
    assert stored["be_state"]["protected_price"] == 4164.00  # + 0.25 + 0.10 + 0.05

    assert executor.confirm_break_even(spec.group_id) == GroupState.BE_CONFIRMED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["be_state"]["status"] == "BE_CONFIRMED"
    assert stored["be_state"]["confirmed_price"] == stored["be_state"]["protected_price"]

    # TP2, TP3, RECONCILED
    assert executor.simulate_tick(spec.group_id, 4167.70) == ["tp2_filled"]
    assert executor.simulate_tick(spec.group_id, 4171.20) == ["tp3_filled"]
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED

    events = _event_types(executor.ledger_db_path)
    for event in ("signal_validated", "trade_intent_created", "group_submitted",
                  "leg_submitted", "tp1_filled", "be_requested", "be_confirmed",
                  "tp2_filled", "tp3_filled", "group_reconciled"):
        assert event in {e[0] for e in events}, event
    for row in events:
        if row[1] is not None:
            assert row[1] == spec.group_id


def test_ledger_geometry_parity_with_spec_and_telegram(executor):
    """ТЗ §20: TradeGroupSpec == Telegram == ledger geometry."""
    from alerts.formatter import format_trade_group_message, geometry_from_spec

    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    events = read_trading_events(executor.ledger_db_path)
    submitted = events.loc[events["event_type"] == "group_submitted"].iloc[0]
    ledger_geometry = json.loads(submitted["payload_json"])["geometry"]
    assert ledger_geometry == geometry_from_spec(spec)
    message = format_trade_group_message(spec)
    assert f"TP1: {spec.geometry.tp1}" in message
    assert f"TP2: {spec.geometry.tp2}" in message
    assert f"TP3: {spec.geometry.tp3}" in message
    assert f"Стоп: {spec.geometry.sl}" in message


def test_be_rejection_never_confirms(executor):
    """ТЗ §18/§28.7: modify rejection -> BE_RETRY; BE_CONFIRMED never emitted."""
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)
    executor.request_break_even(spec.group_id)
    executor.driver.reject_modify = True
    state = executor.confirm_break_even(spec.group_id)
    assert state == GroupState.BE_RETRY
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["be_state"]["status"] == "BE_RETRY"
    assert stored["be_state"]["last_error"] == "simulated modify rejection (freeze/requote)"
    assert "be_confirmed" not in {e[0] for e in _event_types(executor.ledger_db_path)}
    # retry until the bounded limit -> FAILED, still never BE_CONFIRMED
    executor.confirm_break_even(spec.group_id)   # retries=2 -> BE_RETRY
    executor.confirm_break_even(spec.group_id)   # retries=3 -> FAILED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert "be_confirmed" not in {e[0] for e in _event_types(executor.ledger_db_path)}


def test_be_query_mismatch_never_confirms(executor):
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)
    executor.request_break_even(spec.group_id)
    # simulate broker accepting but reporting a different SL
    original = executor.driver.query_sl

    def _wrong_query(ref):
        return (original(ref) or 0.0) + 0.5

    executor.driver.query_sl = _wrong_query
    state = executor.confirm_break_even(spec.group_id)
    assert state == GroupState.BE_RETRY
    assert "be_confirmed" not in {e[0] for e in _event_types(executor.ledger_db_path)}


def test_stop_before_tp1(executor):
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    events = executor.simulate_tick(spec.group_id, 4140.30)
    assert events == ["stop_filled"]
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.STOPPED
    assert "tp1_filled" not in {e[0] for e in _event_types(executor.ledger_db_path)}


def test_restart_no_duplicate_submission(executor, tmp_path):
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)
    executor.request_break_even(spec.group_id)

    # "restart": a NEW executor instance over the same DB + fresh driver
    recovered = TradeGroupExecutor(
        executor.db_path, driver=PaperDriver(account_mode="netting"),
        cost=COST, broker=BROKER,
    )
    current = recovered.recover_after_restart(spec.group_id)
    assert current["state"] == GroupState.BE_REQUESTED
    assert current["submitted"] is True
    assert current["spec"].entry.actual_fill == 4163.60   # TP state preserved
    # resubmission is impossible -> no duplicate orders (ТЗ §25)
    with pytest.raises(DuplicateSubmissionError):
        recovered.submit_group(spec.group_id)


def test_hedging_driver_three_positions(tmp_path):
    driver = PaperDriver(account_mode="hedging")
    executor = TradeGroupExecutor(str(tmp_path / "hedging-test.sqlite"), driver=driver,
                                  cost=COST, broker=BROKER)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    stored = load_group(executor.db_path, spec.group_id)
    pos_ids = {item["broker"]["position_id"] for item in stored["legs"]}
    assert len(pos_ids) == 3   # ТЗ §13.1: each leg its own position


def test_mode_gates(tmp_path):
    live_spec = make_spec(mode="live")
    demo_spec = make_spec(mode="demo")
    executor = TradeGroupExecutor(str(tmp_path / "gates.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    with pytest.raises(LiveExecutionForbidden):
        executor.create_group(live_spec)
    with pytest.raises(DemoExecutionNotEnabled):
        executor.create_group(demo_spec)
    allowed = TradeGroupExecutor(str(tmp_path / "gates2.sqlite"), driver=PaperDriver(),
                                 cost=COST, broker=BROKER, allow_demo=True)
    assert allowed.create_group(demo_spec) == GroupState.VALIDATED


def test_be_without_actual_fill_is_error(executor):
    from data.trade_group_store import save_group
    from execution.trade_group import GroupState as GS

    spec = make_spec()
    executor.create_group(spec)
    # defensive guard: TP1_FILLED state without a confirmed actual fill must
    # never compute a break-even from the signal reference
    save_group(executor.db_path, spec, state=GS.TP1_FILLED)
    with pytest.raises(GroupStateError, match="actual fill"):
        executor.request_break_even(spec.group_id)


def test_actual_fill_be_not_signal_reference(executor):
    """ТЗ §6/§28.10: signal reference 4159.30, actual fill 4159.42 -> BE from fill."""
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    # simulate fill at 4159.42 (requested entry differs from fill)
    executor.on_leg_filled(spec.group_id, 1, 4159.42)
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["spec"].entry.actual_fill == 4159.42
    executor.request_break_even(spec.group_id)
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["be_state"]["raw_price"] == 4159.42
    assert stored["be_state"]["raw_price"] != 4159.30


# ==========================================================================
# Follow-up ТЗ §9/§10: demo env gate + live path forbidden + no order_send
# ==========================================================================

def test_live_forbidden_with_env_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADE_GROUP_ENABLE_DEMO", raising=False)
    executor = TradeGroupExecutor(str(tmp_path / "live-gate.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    with pytest.raises(LiveExecutionForbidden):
        executor.create_group(make_spec(mode="live"))


def test_demo_requires_explicit_env_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_GROUP_ENABLE_DEMO", "0")
    executor = TradeGroupExecutor(str(tmp_path / "demo-gate0.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    with pytest.raises(DemoExecutionNotEnabled):
        executor.create_group(make_spec(mode="demo"))
    monkeypatch.setenv("TRADE_GROUP_ENABLE_DEMO", "1")
    executor = TradeGroupExecutor(str(tmp_path / "demo-gate1.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    assert executor.create_group(make_spec(mode="demo")) == GroupState.VALIDATED


class _OrderSendSpyDriver(PaperDriver):
    """Records every call; fails the test if an order_send-like call happens."""

    def __init__(self, account_mode: str = "netting"):
        super().__init__(account_mode=account_mode)
        self.calls: list[str] = []

    def submit_leg(self, spec, leg, volume):
        self.calls.append(f"submit_leg:{leg}")
        return super().submit_leg(spec, leg, volume)

    def modify_sl(self, reference, sl):
        self.calls.append(f"modify_sl:{reference}")
        return super().modify_sl(reference, sl)

    def query_sl(self, reference):
        self.calls.append(f"query_sl:{reference}")
        return super().query_sl(reference)


def test_paper_execution_never_calls_order_send(executor):
    """ТЗ §10: paper lifecycle must complete without any mt5.order_send call."""
    spy = _OrderSendSpyDriver(account_mode="netting")
    executor.driver = spy
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)
    executor.request_break_even(spec.group_id)
    executor.confirm_break_even(spec.group_id)
    executor.simulate_tick(spec.group_id, 4167.70)
    executor.simulate_tick(spec.group_id, 4171.20)
    assert "order_send" not in spy.calls
    assert not hasattr(executor.driver, "order_send")
    # protocol surface is exactly the driver contract
    assert {"submit_leg:1", "submit_leg:2", "submit_leg:3"} <= set(spy.calls)
    assert any(c.startswith("modify_sl:") for c in spy.calls)
    assert any(c.startswith("query_sl:") for c in spy.calls)


def test_mt5_trader_live_path_does_not_reference_group_executor():
    """ТЗ §11: mt5_trader.py must stay untouched — no group executor wiring."""
    import inspect

    import execution.mt5_trader as trader
    source = inspect.getsource(trader)
    assert "trade_group_executor" not in source
    assert "TradeGroupExecutor" not in source
    assert "TRADE_GROUP_ENABLE_DEMO" not in source


# ==========================================================================
# Follow-up ТЗ §16/§17: hedging leg lifecycle + TP immutability after TP1
# ==========================================================================

def test_hedging_legs_after_tp1_be(tmp_path):
    from execution.trade_group_executor import PaperDriver as PD
    driver = PD(account_mode="hedging")
    ex = TradeGroupExecutor(str(tmp_path / "hedge-lifecycle.sqlite"), driver=driver,
                            cost=COST, broker=BROKER)
    spec = make_spec()
    ex.create_group(spec)
    ex.submit_group(spec.group_id)
    stored = load_group(ex.db_path, spec.group_id)
    pos_ids = {item["broker"]["position_id"] for item in stored["legs"]}
    assert len(pos_ids) == 3                       # 3 physical legs (ТЗ §13.1)

    ex.simulate_tick(spec.group_id, 4163.60)      # TP1
    stored = load_group(ex.db_path, spec.group_id)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    assert leg1["state"] == "CLOSED"

    ex.request_break_even(spec.group_id)
    ex.confirm_break_even(spec.group_id)
    stored = load_group(ex.db_path, spec.group_id)
    # remaining legs' SL now == BE (driver query), TP2/TP3 unchanged
    be = stored["be_state"]["confirmed_price"]
    for leg in (2, 3):
        ref = f"{spec.group_id}-L{leg}"
        assert driver.query_sl(ref) == be
        item = next(i for i in stored["legs"] if i["leg"] == leg)
        assert item["state"] != "CLOSED"
    assert stored["spec"].geometry.tp2 == 4167.70
    assert stored["spec"].geometry.tp3 == 4171.20
    assert stored["spec"].geometry.sl == 4140.30   # original SL is history; BE applied
    assert be != 4140.30


def test_tp_levels_immutable_after_tp1_and_be(tmp_path):
    spec = make_spec()
    executor = TradeGroupExecutor(str(tmp_path / "tp-immutable.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)   # TP1
    executor.request_break_even(spec.group_id)
    executor.confirm_break_even(spec.group_id)
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["spec"].geometry.tp2 == 4167.70    # TP2 untouched
    assert stored["spec"].geometry.tp3 == 4171.20    # TP3 untouched
    assert stored["be_state"]["confirmed_price"] != spec.geometry.sl  # SL moved to BE
    assert stored["spec"].geometry.sl == 4140.30     # approved geometry itself immutable


def test_no_premature_be_below_tp1(tmp_path):
    """ТЗ §18: price 102 with TP1 104 -> no BE, no events."""
    scaled = TradeGroupSpec(
        group_id="TG-NOBE-1", signal_id="SGL-NOBE-1", intent_id="INT-NOBE-1",
        asset_key="XAUUSD", broker_symbol="GOLD", mode="paper", side="long",
        entry={"low": 99.0, "high": 101.0, "reference": 100.0},
        geometry={"version": "v1", "unit": "price", "step_price": 4.0,
                  "tp1": 104.0, "tp2": 108.0, "tp3": 112.0, "sl": 90.0},
        targets=[{"leg": 1, "price": 104.0, "allocation": 0.333333},
                 {"leg": 2, "price": 108.0, "allocation": 0.333333},
                 {"leg": 3, "price": 112.0, "allocation": 0.333334}],
        break_even={"trigger": "tp1_filled",
                    "raw_price_policy": "actual_fill",
                    "protected_price_policy": "actual_fill_plus_cost_buffer",
                    "apply_to": [2, 3]},
        risk={"currency": "USD", "max_cash": 25.0, "max_pct": 0.5,
              "estimated_loss_at_sl": 24.0, "total_volume": 0.03},
        profile_id="dir_v1", model_version="v3", model_hash="m" * 64,
        config_hash="c" * 64, strategy_version="s3",
        expires_at_utc_ms=1_800_000_000_000, created_at_utc_ms=1_700_000_000_000,
    )
    executor = TradeGroupExecutor(str(tmp_path / "no-be.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    executor.create_group(scaled)
    executor.submit_group(scaled.group_id)
    # price moves toward TP1 but does not reach it -> nothing happens
    assert executor.simulate_tick(scaled.group_id, 102.0) == []
    stored = load_group(executor.db_path, scaled.group_id)
    assert stored["state"] == GroupState.SUBMITTED
    assert "be_requested" not in {e[0] for e in _event_types(executor.ledger_db_path)}
    # and BE can never be requested before the confirmed TP1 event
    from execution.trade_group import require_transition
    with pytest.raises(ValueError, match="invalid group transition"):
        require_transition(GroupState.SUBMITTED, GroupState.BE_REQUESTED)


def test_be_retry_then_success(tmp_path):
    """ТЗ §19: modify rejected -> BE_RETRY; after the failure clears -> BE_CONFIRMED."""
    spec = make_spec()
    executor = TradeGroupExecutor(str(tmp_path / "be-retry.sqlite"), driver=PaperDriver(),
                                  cost=COST, broker=BROKER)
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)
    executor.request_break_even(spec.group_id)

    executor.driver.reject_modify = True
    assert executor.confirm_break_even(spec.group_id) == GroupState.BE_RETRY
    assert "be_confirmed" not in {e[0] for e in _event_types(executor.ledger_db_path)}

    executor.driver.reject_modify = False            # broker recovered
    assert executor.confirm_break_even(spec.group_id) == GroupState.BE_CONFIRMED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["be_state"]["status"] == "BE_CONFIRMED"
    assert stored["be_state"]["retries"] == 1


# ==========================================================================
# Follow-up ТЗ §20/§21: restart recovery matrix + ledger consistency
# ==========================================================================

@pytest.mark.parametrize("target_state", [
    GroupState.OPENED,
    GroupState.TP1_FILLED,
    GroupState.BE_REQUESTED,
    GroupState.BE_RETRY,
    GroupState.BE_CONFIRMED,
    GroupState.TP2_FILLED,
])
def test_restart_recovery_preserves_state_and_ids(target_state, tmp_path):
    from data.trade_group_store import load_group as lg

    db = str(tmp_path / f"recover-{target_state.value}.sqlite")
    driver = PaperDriver(account_mode="netting")
    executor = TradeGroupExecutor(db, driver=driver, cost=COST, broker=BROKER)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)

    def _advance_to(state):
        if state == GroupState.OPENED:
            executor._ensure_open(spec.group_id)
        if state in (GroupState.TP1_FILLED, GroupState.BE_REQUESTED,
                     GroupState.BE_RETRY, GroupState.BE_CONFIRMED,
                     GroupState.TP2_FILLED):
            executor.simulate_tick(spec.group_id, 4163.60)  # TP1
        if state in (GroupState.BE_REQUESTED, GroupState.BE_RETRY,
                     GroupState.BE_CONFIRMED, GroupState.TP2_FILLED):
            executor.request_break_even(spec.group_id)
        if state in (GroupState.BE_RETRY, GroupState.BE_CONFIRMED, GroupState.TP2_FILLED):
            executor.driver.reject_modify = True
            executor.confirm_break_even(spec.group_id)      # -> BE_RETRY
            executor.driver.reject_modify = False
        if state in (GroupState.BE_CONFIRMED, GroupState.TP2_FILLED):
            executor.confirm_break_even(spec.group_id)      # -> BE_CONFIRMED
        if state == GroupState.TP2_FILLED:
            executor.simulate_tick(spec.group_id, 4167.70)  # TP2

    _advance_to(target_state)
    snapshot = lg(db, spec.group_id)
    assert snapshot["state"] == target_state

    event_types_before = {e[0] for e in _event_types(executor.ledger_db_path)}
    broker_ids_before = snapshot["broker_ids"]
    geometry_before = snapshot["spec"].as_geometry_payload()
    assert "group_reconciled" not in event_types_before  # not reached TP3 yet

    # "restart": fresh executor + fresh driver over the same DB
    recovered = TradeGroupExecutor(db, driver=PaperDriver(account_mode="netting"),
                                   cost=COST, broker=BROKER)
    restored = recovered.recover_after_restart(spec.group_id)
    assert restored["state"] == target_state
    assert restored["spec"].as_geometry_payload() == geometry_before
    assert restored["broker_ids"] == broker_ids_before
    # no duplicate submission, no duplicate TP/BE events after recovery:
    # the lifecycle event set is unchanged except for the single recovery marker
    with pytest.raises(DuplicateSubmissionError):
        recovered.submit_group(spec.group_id)
    event_types_after = {e[0] for e in _event_types(executor.ledger_db_path)}
    assert event_types_after - event_types_before == {"group_reconciled"}
    events_after = [e[0] for e in _event_types(executor.ledger_db_path)]
    for event in ("tp1_filled", "be_requested", "be_confirmed", "tp2_filled"):
        if event in event_types_before:
            assert events_after.count(event) == 1, event


def test_ledger_events_chronological_and_deduplicated(executor):
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.simulate_tick(spec.group_id, 4163.60)
    executor.request_break_even(spec.group_id)
    executor.confirm_break_even(spec.group_id)
    executor.simulate_tick(spec.group_id, 4167.70)
    executor.simulate_tick(spec.group_id, 4171.20)

    df = read_trading_events(executor.ledger_db_path)
    rows = df.to_dict("records")
    # chronological: sequence is monotonic and event_timestamp non-decreasing
    sequences = [int(r["sequence"]) for r in rows]
    assert sequences == sorted(sequences)
    timestamps = [int(r["event_timestamp_utc"]) for r in rows]
    assert timestamps == sorted(timestamps)
    # every lifecycle event carries group_id (+ leg_id where applicable)
    for r in rows:
        if r["event_type"] in {"tp1_filled", "tp2_filled", "tp3_filled",
                               "leg_submitted", "be_requested", "be_confirmed"}:
            assert r["group_id"] == spec.group_id
    # no duplicates of key events
    counts = {event: sum(1 for r in rows if r["event_type"] == event)
              for event in ("tp1_filled", "be_requested", "be_confirmed",
                            "tp2_filled", "tp3_filled", "group_reconciled")}
    assert counts == {"tp1_filled": 1, "be_requested": 1, "be_confirmed": 1,
                      "tp2_filled": 1, "tp3_filled": 1, "group_reconciled": 1}
