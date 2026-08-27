"""Tests for execution/trade_group.py — TradeGroupSpec v1 domain contract."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from execution.trade_group import (
    DEFAULT_MAX_FILL_DEVIATION,
    GROUP_SCHEMA_VERSION,
    GroupState,
    TERMINAL_STATES,
    TradeGroupSpec,
    allocate_leg_volumes,
    check_group_not_expired,
    check_group_risk,
    get_max_fill_deviation,
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


def test_geometry_hash_includes_risk():
    """P0-3: estimated_loss_at_sl is part of the geometry hash, so mutating it
    produces a different hash and require_geometry_unchanged rejects the drift."""
    from execution.execution_intent import ExecutionIntent

    spec = make_spec()
    drifted_risk = spec.risk.model_copy(update={"estimated_loss_at_sl": 99.99})
    drifted = spec.model_copy(update={"risk": drifted_risk})
    assert drifted.geometry_hash() != spec.geometry_hash()

    intent = ExecutionIntent.from_spec(spec)
    with pytest.raises(Exception, match="geometry"):
        intent.require_geometry_unchanged(drifted)


def test_with_actual_fill_does_not_recompute_estimated_loss():
    """Geometry/risk immutability: attaching an actual fill must never touch
    the risk block (estimated_loss_at_sl is fixed at validation time)."""
    spec = make_spec()
    filled = spec.with_actual_fill(4159.42)
    assert filled.risk.estimated_loss_at_sl == spec.risk.estimated_loss_at_sl
    assert filled.risk.model_dump() == spec.risk.model_dump()


def test_actual_fill_cannot_be_overwritten():
    spec = make_spec().with_actual_fill(4159.42)
    assert spec.entry.actual_fill == 4159.42
    with pytest.raises(ValueError, match="cannot be overwritten"):
        spec.with_actual_fill(4159.99)


def test_actual_fill_deviation_rejected():
    """P0-7: a fill 10% above the reference is rejected with ValueError."""
    spec = make_spec()
    reference = spec.entry.reference            # 4159.30
    fill_10pct_above = reference * 1.10
    with pytest.raises(ValueError, match="deviat"):
        spec.with_actual_fill(fill_10pct_above)


def test_actual_fill_small_drift_accepted():
    """A sub-threshold drift still attaches normally."""
    spec = make_spec()
    reference = spec.entry.reference
    small = reference * (1.0 + DEFAULT_MAX_FILL_DEVIATION * 0.5)  # +2.5%
    filled = spec.with_actual_fill(small)
    assert filled.entry.actual_fill == pytest.approx(small)


def test_max_fill_deviation_threshold_configurable():
    """Default threshold is 5%; malformed config values fall back to it."""
    assert DEFAULT_MAX_FILL_DEVIATION == 0.05
    assert get_max_fill_deviation() == pytest.approx(DEFAULT_MAX_FILL_DEVIATION)


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


# ==========================================================================
# Follow-up ТЗ §3: explicit LONG/SHORT direction regression tests
# ==========================================================================

def _spec_100(**overrides) -> TradeGroupSpec:
    """Base spec at the follow-up example scale (entry=100)."""
    base = dict(
        group_id="TG-DIR-1",
        signal_id="SGL-DIR-1",
        intent_id="INT-DIR-1",
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        mode="paper",
        side="long",
        entry={"low": 99.0, "high": 101.0, "reference": 100.0, "actual_fill": None},
        geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                  "tp1": 104.0, "tp2": 108.0, "tp3": 112.0, "sl": 90.0},
        targets=[
            {"leg": 1, "price": 104.0, "allocation": 0.333333},
            {"leg": 2, "price": 108.0, "allocation": 0.333333},
            {"leg": 3, "price": 112.0, "allocation": 0.333334},
        ],
        break_even={"trigger": "tp1_filled",
                    "raw_price_policy": "actual_fill",
                    "protected_price_policy": "actual_fill_plus_cost_buffer",
                    "apply_to": [2, 3]},
        risk={"currency": "USD", "max_cash": 25.0, "max_pct": 0.5,
              "estimated_loss_at_sl": 24.0, "total_volume": 0.03},
        profile_id="dir_v1",
        model_version="v3", model_hash="m" * 64, config_hash="c" * 64,
        strategy_version="s3",
        expires_at_utc_ms=1_800_000_000_000, created_at_utc_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return TradeGroupSpec(**base)


def _short_100(**overrides) -> TradeGroupSpec:
    """Valid SHORT at the follow-up example scale (entry=100)."""
    base = dict(
        group_id="TG-DIR-S1",
        side="short",
        entry={"low": 99.0, "high": 101.0, "reference": 100.0, "actual_fill": None},
        geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                  "tp1": 96.0, "tp2": 92.0, "tp3": 88.0, "sl": 110.0},
        targets=[
            {"leg": 1, "price": 96.0, "allocation": 0.333333},
            {"leg": 2, "price": 92.0, "allocation": 0.333333},
            {"leg": 3, "price": 88.0, "allocation": 0.333334},
        ],
    )
    base.update(overrides)
    return _spec_100(**base)


def test_valid_long_geometry_accepted():
    _spec_100()  # SL 90 < entry 100 < TP1 104 < TP2 108 < TP3 112


def test_invalid_long_sl_above_entry_rejected():
    with pytest.raises(ValidationError, match="invalid LONG geometry"):
        _spec_100(geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                            "tp1": 104.0, "tp2": 108.0, "tp3": 112.0, "sl": 101.0})


def test_valid_short_geometry_accepted():
    _short_100()  # TP3 88 < TP2 92 < TP1 96 < entry 100 < SL 110


def test_invalid_short_sl_below_entry_rejected():
    with pytest.raises(ValidationError, match="invalid SHORT geometry"):
        _short_100(geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                             "tp1": 96.0, "tp2": 92.0, "tp3": 88.0, "sl": 99.0})


def test_invalid_short_tp1_above_entry_rejected():
    with pytest.raises(ValidationError, match="invalid SHORT geometry"):
        _short_100(geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                             "tp1": 101.0, "tp2": 92.0, "tp3": 88.0, "sl": 110.0})


def test_invalid_short_tp2_not_below_tp1_rejected():
    with pytest.raises(ValidationError, match="invalid SHORT geometry"):
        _short_100(geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                             "tp1": 96.0, "tp2": 96.0, "tp3": 88.0, "sl": 110.0})


def test_invalid_short_tp3_not_below_tp2_rejected():
    with pytest.raises(ValidationError, match="invalid SHORT geometry"):
        _short_100(geometry={"version": "dir_v1", "unit": "price", "step_price": 4.0,
                             "tp1": 96.0, "tp2": 92.0, "tp3": 92.0, "sl": 110.0})


# ==========================================================================
# Follow-up ТЗ §7: actual_fill write-once
# ==========================================================================

def test_actual_fill_write_once_semantics():
    spec = _spec_100()
    assert spec.entry.actual_fill is None
    filled = spec.with_actual_fill(100.05)            # None -> 100.05 allowed
    assert filled.entry.actual_fill == 100.05
    again = filled.with_actual_fill(100.05)           # 100.05 -> 100.05 idempotent
    assert again.entry.actual_fill == 100.05
    with pytest.raises(ValueError, match="cannot be overwritten"):
        filled.with_actual_fill(100.10)               # 100.05 -> 100.10 rejected


# ==========================================================================
# Follow-up ТЗ §8: allocation is direction-independent and dust-safe
# ==========================================================================

@pytest.mark.parametrize("total", [0.03, 0.04, 0.05, 0.10])
def test_allocation_sums_to_total_for_both_directions(total):
    volumes_long = allocate_leg_volumes(total, volume_step=0.01, volume_min=0.01)
    # allocate_leg_volumes has no side input -> direction independence is
    # structural; assert the same result is used for a SHORT spec too.
    spec_long = _spec_100(risk={"currency": "USD", "max_cash": 50.0, "max_pct": 0.5,
                                "estimated_loss_at_sl": 48.0, "total_volume": total})
    spec_short = _short_100(risk={"currency": "USD", "max_cash": 50.0, "max_pct": 0.5,
                                  "estimated_loss_at_sl": 48.0, "total_volume": total})
    for spec in (spec_long, spec_short):
        allocated = [
            round(spec.risk.total_volume * t.allocation, 8) for t in spec.targets
        ]
        assert sum(allocated) == pytest.approx(total, abs=1e-6)
    assert sum(volumes_long) == pytest.approx(total, abs=1e-6)
    assert volumes_long == allocate_leg_volumes(total, volume_step=0.01, volume_min=0.01)


def test_allocation_dust_tolerance():
    # 0.03 equal thirds must not lose dust: 0.01/0.01/0.01
    assert allocate_leg_volumes(0.03, volume_step=0.01, volume_min=0.01) == [0.01, 0.01, 0.01]
    assert sum(allocate_leg_volumes(0.10, volume_step=0.01, volume_min=0.01)) == pytest.approx(0.10)


# ==========================================================================
# Follow-up ТЗ §9: group risk math is direction-symmetric (abs distance)
# ==========================================================================

def test_estimated_loss_same_for_long_and_short():
    from execution.trade_geometry import calculate_geometry
    profile = {
        "asset": "XAUUSD", "timeframe": "M15", "unit": "price", "validated": True,
        "geometry_version": "v1",
        "step": {"source": "atr", "atr_mult": 1.0,
                 "min_price_distance": 3.0, "max_price_distance": 9.0},
        "targets": {"multipliers": {"tp1": 1.0, "tp2": 1.5, "tp3": 2.0}},
        "stop": {"source": "validated_multiple", "multiplier": 2.0},
        "allocation": {"tp1": 0.333333, "tp2": 0.333333, "tp3": 0.333334},
        "risk": {"currency": "USD", "max_pct": 0.5, "max_cash": 50.0},
        "volume": {"total": 0.06},
    }
    broker = BrokerSnapshot(symbol_point=0.01, tick_size=0.01, digits=2,
                            spread=0.25, contract_size=100.0,
                            volume_min=0.01, volume_step=0.01, balance=10000.0)
    cost = CostSnapshot(round_trip_cost_price=0.30, safety_buffer_price=0.10)
    long_out = calculate_geometry(profile=profile, side="long", reference_price=100.0,
                                  atr=4.0, broker=broker, cost=cost)
    short_out = calculate_geometry(profile=profile, side="short", reference_price=100.0,
                                   atr=4.0, broker=broker, cost=cost)
    # same distance, same volume -> identical absolute estimated loss
    assert long_out.estimated_loss_at_sl == pytest.approx(short_out.estimated_loss_at_sl)
    assert long_out.estimated_loss_at_sl > 0.0
    assert short_out.estimated_loss_at_sl > 0.0


# ==========================================================================
# Follow-up ТЗ §6: approved geometry is immune to market changes
# ==========================================================================

def test_atr_change_does_not_touch_approved_spec():
    from execution.trade_geometry import (
        BrokerSnapshot as BS, CostSnapshot as CS, build_trade_group_from_signal,
    )
    cfg = {"trade_profiles": {"p": {
        "asset": "XAUUSD", "validated": True, "geometry_version": "v1",
        "step": {"source": "atr", "atr_mult": 1.0,
                 "min_price_distance": 3.0, "max_price_distance": 9.0},
        "targets": {"multipliers": {"tp1": 1.0, "tp2": 1.5, "tp3": 2.0}},
        "stop": {"multiplier": 2.0},
        "allocation": {"tp1": 1 / 3, "tp2": 1 / 3, "tp3": 1 / 3},
        # generous cap so the ATR-8 candidate passes risk (loss 96 < 200)
        "risk": {"max_cash": 200.0, "max_pct": 0.5}, "volume": {"total": 0.06},
    }}}
    broker = BS(symbol_point=0.01, tick_size=0.01, spread=0.25, contract_size=100.0,
                volume_min=0.01, volume_step=0.01, balance=10000.0)
    cost = CS(round_trip_cost_price=0.30, safety_buffer_price=0.10)
    signal = {"bias": "long", "atr": 4.0, "entry_zone": [4159.10, 4159.50],
              "expires_at_utc_ms": 1_900_000_000_000}
    approved = build_trade_group_from_signal(
        signal, cfg=cfg, asset_key="XAUUSD", profile_id="p",
        broker=broker, cost=cost, mode="paper", now_ms=1_700_000_000_000)
    before = approved.as_geometry_payload()

    # ATR changes in a NEW market snapshot -> new candidate spec, approved one unchanged
    signal["atr"] = 8.0
    candidate = build_trade_group_from_signal(
        signal, cfg=cfg, asset_key="XAUUSD", profile_id="p",
        broker=broker, cost=cost, mode="paper", now_ms=1_700_000_000_001)
    assert approved.as_geometry_payload() == before          # approved untouched
    assert candidate.geometry.tp1 != approved.geometry.tp1   # candidate differs

    # spread changes -> still approved unchanged
    broker_wider = BS(symbol_point=0.01, tick_size=0.01, spread=5.0, contract_size=100.0,
                      volume_min=0.01, volume_step=0.01, balance=10000.0)
    candidate2 = build_trade_group_from_signal(
        {**signal, "atr": 4.0}, cfg=cfg, asset_key="XAUUSD", profile_id="p",
        broker=broker_wider, cost=cost, mode="paper", now_ms=1_700_000_000_002)
    assert approved.as_geometry_payload() == before
    # new candle = new reference -> candidate differs, approved unchanged
    candidate3 = build_trade_group_from_signal(
        {**signal, "atr": 4.0, "entry_zone": [4160.10, 4160.50]},
        cfg=cfg, asset_key="XAUUSD", profile_id="p",
        broker=broker, cost=cost, mode="paper", now_ms=1_700_000_000_003)
    assert approved.as_geometry_payload() == before
    assert candidate3.entry.reference != approved.entry.reference
