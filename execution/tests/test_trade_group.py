"""Tests for execution/trade_group.py — TradeGroupSpec v1 domain contract."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from execution.trade_group import (
    GROUP_SCHEMA_VERSION,
    GroupState,
    TERMINAL_STATES,
    TradeGroupSpec,
    allocate_leg_volumes,
    check_group_not_expired,
    check_group_risk,
    new_group_id,
    new_intent_id,
    new_leg_id,
    require_transition,
    validate_transition,
)
from execution.trade_geometry import compute_break_even, calculate_gross_r
from execution.trade_geometry import BrokerSnapshot, CostSnapshot


def make_spec(**overrides) -> TradeGroupSpec:
    base = dict(
        group_id="TG-20260816-000042",
        signal_id="SGL-20260816-000042",
        intent_id="INT-20260816-000042",
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        mode="paper",
        side="long",
        entry={"low": 4159.10, "high": 4159.50, "reference": 4159.30, "actual_fill": None},
        geometry={"version": "xau_m15_intraday_v1", "unit": "price",
                  "step_price": 4.30, "tp1": 4163.60, "tp2": 4167.70,
                  "tp3": 4171.20, "sl": 4140.30},
        targets=[
            {"leg": 1, "price": 4163.60, "allocation": 0.333333},
            {"leg": 2, "price": 4167.70, "allocation": 0.333333},
            {"leg": 3, "price": 4171.20, "allocation": 0.333334},
        ],
        break_even={"trigger": "tp1_filled",
                    "raw_price_policy": "actual_fill",
                    "protected_price_policy": "actual_fill_plus_cost_buffer",
                    "apply_to": [2, 3]},
        risk={"currency": "USD", "max_cash": 25.0, "max_pct": 0.50,
              "estimated_loss_at_sl": 24.73, "total_volume": 0.03},   # ТЗ example form
        profile_id="xau_m15_intraday_v1",
        model_version="v3",
        model_hash="m" * 64,
        config_hash="c" * 64,
        strategy_version="xauusd-system-v3-signalbar-2026-08-16",
        expires_at_utc_ms=1_800_000_000_000,
        created_at_utc_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return TradeGroupSpec(**base)


# --------------------------------------------------------------------------
# Contract validation (ТЗ §5)
# --------------------------------------------------------------------------

def test_required_fields_enforced():
    with pytest.raises(ValidationError):
        make_spec(group_id="")
    with pytest.raises(ValidationError):
        make_spec(model_version="")
    with pytest.raises(ValidationError):
        make_spec(expires_at_utc_ms=0)
    with pytest.raises(ValidationError):
        make_spec(side="sideways")


def test_geometry_direction_consistent():
    # long: TP ladder above entry, SL below
    with pytest.raises(ValidationError):
        make_spec(side="long", geometry={**make_spec().geometry.model_dump(),
                                         "tp1": 4150.0})
    with pytest.raises(ValidationError):
        make_spec(side="long", geometry={**make_spec().geometry.model_dump(),
                                         "sl": 4200.0})
    with pytest.raises(ValidationError):
        make_spec(side="long", geometry={**make_spec().geometry.model_dump(),
                                         "tp2": 4160.0})  # below tp1
    # short mirror
    short = make_spec(side="short",
                      entry={**make_spec().entry.model_dump(), "reference": 4159.30,
                             "low": 4159.10, "high": 4159.50},
                      geometry={"version": "v", "unit": "price", "step_price": 4.30,
                                "tp1": 4155.00, "tp2": 4150.70, "tp3": 4146.40,
                                "sl": 4179.30})
    assert short.side == "short"


def test_allocations_must_sum_to_one_and_be_three_legs():
    with pytest.raises(ValidationError):
        make_spec(targets=[
            {"leg": 1, "price": 4163.60, "allocation": 0.5},
            {"leg": 2, "price": 4167.70, "allocation": 0.5},
            {"leg": 3, "price": 4171.20, "allocation": 0.1},
        ])
    with pytest.raises(ValidationError):
        make_spec(targets=[
            {"leg": 1, "price": 4163.60, "allocation": 1.0},
        ])


def test_schema_version_is_trade_group_v1():
    assert make_spec().schema_version == GROUP_SCHEMA_VERSION


# --------------------------------------------------------------------------
# Immutability (ТЗ §12)
# --------------------------------------------------------------------------

def test_geometry_is_immutable_after_creation():
    spec = make_spec()
    with pytest.raises(ValidationError):  # frozen pydantic model
        spec.geometry.tp1 = 9999.0
    with pytest.raises(ValidationError):
        spec.risk.max_cash = 0.0


def test_geometry_hash_stable_across_actual_fill():
    spec = make_spec()
    filled = spec.with_actual_fill(4159.42)
    assert filled.geometry.tp1 == spec.geometry.tp1
    assert filled.geometry.sl == spec.geometry.sl
    assert filled.geometry_hash() == spec.geometry_hash()
    # canonical hash (full snapshot) changes: actual fill is part of the state
    assert filled.canonical_hash() != spec.canonical_hash()


def test_actual_fill_cannot_be_overwritten():
    spec = make_spec().with_actual_fill(4159.42)
    assert spec.entry.actual_fill == 4159.42
    with pytest.raises(ValueError, match="cannot be overwritten"):
        spec.with_actual_fill(4159.99)


# --------------------------------------------------------------------------
# State machine (ТЗ §23)
# --------------------------------------------------------------------------

def test_state_machine_happy_path():
    order = [GroupState.DRAFT, GroupState.VALIDATED, GroupState.SUBMITTED,
             GroupState.OPENED, GroupState.TP1_FILLED, GroupState.BE_REQUESTED,
             GroupState.BE_CONFIRMED, GroupState.TP2_FILLED, GroupState.TP3_FILLED,
             GroupState.RECONCILED]
    for current, nxt in zip(order, order[1:]):
        assert validate_transition(current, nxt), f"{current} -> {nxt}"
        require_transition(current, nxt)


def test_state_machine_rejects_skips_and_invalid_moves():
    with pytest.raises(ValueError, match="invalid group transition"):
        require_transition(GroupState.DRAFT, GroupState.SUBMITTED)
    with pytest.raises(ValueError, match="invalid group transition"):
        require_transition(GroupState.TP1_FILLED, GroupState.TP2_FILLED)  # BE required
    with pytest.raises(ValueError, match="invalid group transition"):
        require_transition(GroupState.TP1_FILLED, GroupState.TP3_FILLED)
    with pytest.raises(ValueError, match="invalid group transition"):
        require_transition(GroupState.RECONCILED, GroupState.DRAFT)  # terminal


def test_terminal_states_have_no_outgoing():
    for state in TERMINAL_STATES:
        assert not validate_transition(state, GroupState.DRAFT)
        assert not validate_transition(state, GroupState.RECONCILED)


# --------------------------------------------------------------------------
# Volume allocation (ТЗ §15)
# --------------------------------------------------------------------------

def test_volume_allocation_floor_rule_matches_spec_example():
    # ТЗ §15 example: total 0.05 -> 0.01 / 0.01 / 0.03
    volumes = allocate_leg_volumes(0.05, (1 / 3, 1 / 3, 1 / 3),
                                   volume_step=0.01, volume_min=0.01)
    assert volumes == [0.01, 0.01, 0.03]
    assert sum(volumes) == pytest.approx(0.05)


def test_volume_allocation_three_equal_legs():
    volumes = allocate_leg_volumes(0.03, volume_step=0.01, volume_min=0.01)
    assert volumes == [0.01, 0.01, 0.01]


def test_volume_allocation_rejects_unfillable_legs():
    with pytest.raises(ValueError, match="INSUFFICIENT_VOLUME_FOR_THREE_LEGS"):
        allocate_leg_volumes(0.005, volume_step=0.01, volume_min=0.01)
    with pytest.raises(ValueError, match="INSUFFICIENT_VOLUME_FOR_THREE_LEGS"):
        allocate_leg_volumes(0.03, (0.1, 0.1, 0.8),  # leg2 = floor(0.003) = 0
                             volume_step=0.01, volume_min=0.01)
    # explicit netting fallback is allowed and visible
    volumes = allocate_leg_volumes(0.03, (0.1, 0.1, 0.8),
                                   volume_step=0.01, volume_min=0.01,
                                   allow_short_legs=True)
    assert volumes == [0.0, 0.0, 0.03]


# --------------------------------------------------------------------------
# Group risk (ТЗ §14): ONE check for the whole group
# --------------------------------------------------------------------------

def test_group_risk_single_cap_not_per_leg():
    ok, reason = check_group_risk(estimated_loss_at_sl=24.0, max_cash=25.0,
                                  max_pct=0.01, balance=10000.0)
    assert ok is True and reason is None
    # 3x intended risk must fail (simulating per-leg miscalc)
    ok, reason = check_group_risk(estimated_loss_at_sl=72.0, max_cash=25.0,
                                  max_pct=0.01, balance=10000.0)
    assert ok is False and reason == "RISK_LIMIT_EXCEEDED"
    # pct cap also enforced
    ok, reason = check_group_risk(estimated_loss_at_sl=200.0, max_cash=10000.0,
                                  max_pct=0.01, balance=10000.0)
    assert ok is False and reason == "RISK_LIMIT_EXCEEDED"


def test_expiry_check():
    assert check_group_not_expired(1_000, 999) is True
    assert check_group_not_expired(1_000, 1_001) is False


# --------------------------------------------------------------------------
# Ids (ТЗ §24)
# --------------------------------------------------------------------------

def test_identifier_separation():
    spec = make_spec()
    assert spec.signal_id.startswith("SGL-")
    assert spec.intent_id.startswith("INT-")
    assert spec.group_id.startswith("TG-")
    assert new_leg_id(spec.group_id, 1) == f"{spec.group_id}-L1"
    assert new_leg_id(spec.group_id, 3) == f"{spec.group_id}-L3"
    assert new_group_id(1_700_000_000_000).startswith("TG-")
    assert new_intent_id(1_700_000_000_000).startswith("INT-")


# --------------------------------------------------------------------------
# Gross R (ТЗ §11 example form)
# --------------------------------------------------------------------------

def test_gross_r_example_form():
    spec = make_spec()
    r = calculate_gross_r(spec.side, spec.entry.reference, spec.geometry.sl,
                          spec.targets)
    assert r == pytest.approx(0.432, abs=0.002)


# --------------------------------------------------------------------------
# Break-even (ТЗ §17): actual fill only
# --------------------------------------------------------------------------

def test_break_even_uses_actual_fill_long():
    broker = BrokerSnapshot(symbol_point=0.01, tick_size=0.01, digits=2,
                            spread=0.25, contract_size=100.0)
    cost = CostSnapshot(expected_exit_slippage=0.10, commission_buffer=0.05)
    be = compute_break_even(side="long", actual_fill=4159.42, cost=cost, broker=broker)
    assert be["raw_price"] == 4159.42          # actual fill, not signal reference
    assert be["protected_price"] == 4159.82    # + 0.25 spread + 0.10 slip + 0.05 comm
    assert be["raw_price"] != 4159.30


def test_break_even_short_mirror():
    broker = BrokerSnapshot(symbol_point=0.01, tick_size=0.01, spread=0.25)
    cost = CostSnapshot(expected_exit_slippage=0.10, commission_buffer=0.05)
    be = compute_break_even(side="short", actual_fill=4159.42, cost=cost, broker=broker)
    assert be["raw_price"] == 4159.42
    assert be["protected_price"] == 4159.02
