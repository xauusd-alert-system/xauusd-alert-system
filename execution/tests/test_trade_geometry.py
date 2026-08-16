"""Tests for execution/trade_geometry.py — pure deterministic geometry engine."""
from __future__ import annotations

import pytest

from execution.trade_geometry import (
    BrokerSnapshot,
    CostSnapshot,
    GeometryRejected,
    INSUFFICIENT_VOLUME_FOR_THREE_LEGS,
    INVALID_TICK_ALIGNMENT,
    PROFILE_NOT_VALIDATED,
    RISK_LIMIT_EXCEEDED,
    SIGNAL_EXPIRED,
    STOP_BELOW_BROKER_MIN_DISTANCE,
    TP1_TOO_CLOSE_TO_COST,
    align_to_tick,
    build_trade_group_from_signal,
    calculate_geometry,
    calculate_gross_r,
    calculate_step,
    is_tick_aligned,
    resolve_profile,
    terminal_points,
    validate_profile_gate,
)

XAU_PROFILE = {
    "asset": "XAUUSD", "timeframe": "M15", "unit": "price", "validated": True,
    "geometry_version": "xau_m15_intraday_v1",
    "step": {"source": "atr", "atr_mult": 1.0,
             "min_price_distance": 3.0, "max_price_distance": 9.0},
    "targets": {"multipliers": {"tp1": 1.0, "tp2": 1.5, "tp3": 2.0}},
    "stop": {"source": "validated_multiple", "multiplier": 2.0},
    "break_even": {"trigger": "tp1_filled",
                   "raw_price_policy": "actual_fill",
                   "protected_price_policy": "actual_fill_plus_cost_buffer",
                   "apply_to": [2, 3]},
    "allocation": {"tp1": 0.333333, "tp2": 0.333333, "tp3": 0.333334},
    "risk": {"currency": "USD", "max_pct": 0.50, "max_cash": 50.0},
    "volume": {"total": 0.06},   # floor rule -> legs 0.01/0.01/0.04
}

BTC_CANDIDATE = {
    "asset": "BTCUSD", "timeframe": "M5", "unit": "price", "validated": False,
    "validation_status": "pending_btc_validation", "paper_only": True,
    "step": {"source": "atr", "atr_mult": 1.0,
             "min_price_distance": 4.0, "max_price_distance": 6.0},
    "targets": {"multipliers": {"tp1": 1.0, "tp2": 2.0, "tp3": 3.0}},
    "stop": {"source": "structure_or_validated_multiple", "multiplier": 2.0},
    "allocation": {"tp1": 0.333333, "tp2": 0.333333, "tp3": 0.333334},
    "risk": {"currency": "USD", "max_pct": 0.50, "max_cash": 25.0},
    "volume": {"total": 0.01},
}

CFG = {"trade_profiles": {"xau_m15_intraday_v1": XAU_PROFILE,
                          "btc_m5_scalp_v1": BTC_CANDIDATE}}

BROKER_XAU = BrokerSnapshot(
    symbol_point=0.01, tick_size=0.01, digits=2,
    trade_stops_level=0, trade_freeze_level=0, spread=0.25,
    contract_size=100.0, volume_min=0.01, volume_max=10.0, volume_step=0.01,
    execution_mode="request", account_margin_mode="netting", balance=10000.0,
)

COST_XAU = CostSnapshot(round_trip_cost_price=0.30, safety_buffer_price=0.10,
                        expected_exit_slippage=0.10, commission_buffer=0.05)


def test_resolve_profile_explicit_and_auto():
    profile = resolve_profile(CFG, "XAUUSD", "xau_m15_intraday_v1")
    assert profile["asset"] == "XAUUSD"
    assert resolve_profile(CFG, "XAUUSD")["asset"] == "XAUUSD"
    with pytest.raises(ValueError, match="unknown trade profile"):
        resolve_profile(CFG, "XAUUSD", "nope")
    with pytest.raises(ValueError):
        resolve_profile({}, "XAUUSD")


def test_validation_gate_blocks_btc_candidate():
    with pytest.raises(GeometryRejected) as exc:
        validate_profile_gate(BTC_CANDIDATE)
    assert exc.value.reason_code == PROFILE_NOT_VALIDATED
    validate_profile_gate(XAU_PROFILE)  # validated profile passes


def test_calculate_step_atr_clamps():
    profile = XAU_PROFILE
    assert calculate_step(profile, 4.0) == 4.0
    assert calculate_step(profile, 1.0) == 3.0    # min clamp
    assert calculate_step(profile, 20.0) == 9.0   # max clamp


def test_geometry_example_shape():
    out = calculate_geometry(
        profile=XAU_PROFILE, side="long", reference_price=4159.30,
        atr=4.0, broker=BROKER_XAU, cost=COST_XAU,
    )
    assert out.tp1 == 4163.30
    assert out.tp2 == 4165.30
    assert out.tp3 == 4167.30
    assert out.sl == 4151.30
    assert out.step_price == 4.0
    assert out.leg_volumes == [0.01, 0.01, 0.04]   # floor rule: leg3 = remainder
    assert out.estimated_loss_at_sl == pytest.approx(0.06 * 8.0 * 100.0)
    for price in (out.tp1, out.tp2, out.tp3, out.sl):
        assert is_tick_aligned(price, BROKER_XAU.tick_size)
    # short mirror
    out_short = calculate_geometry(
        profile=XAU_PROFILE, side="short", reference_price=4159.30,
        atr=4.0, broker=BROKER_XAU, cost=COST_XAU,
    )
    assert out_short.sl > 4159.30 and out_short.tp1 < 4159.30


def test_tp1_too_close_to_cost_rejects():
    cost = CostSnapshot(round_trip_cost_price=5.0, safety_buffer_price=1.0)
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(profile=XAU_PROFILE, side="long", reference_price=4159.30,
                           atr=4.0, broker=BROKER_XAU, cost=cost)
    assert exc.value.reason_code == TP1_TOO_CLOSE_TO_COST


def test_stop_below_broker_min_distance_rejects():
    broker = BrokerSnapshot(
        symbol_point=0.01, tick_size=0.01, digits=2,
        trade_stops_level=1000, trade_freeze_level=0, spread=0.25,  # 10.0 price min
        contract_size=100.0, volume_step=0.01, volume_min=0.01, balance=10000.0,
    )
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(profile=XAU_PROFILE, side="long", reference_price=4159.30,
                           atr=4.0, broker=broker, cost=COST_XAU)
    assert exc.value.reason_code == STOP_BELOW_BROKER_MIN_DISTANCE


def test_risk_limit_exceeded_rejects():
    profile = {**XAU_PROFILE, "risk": {"currency": "USD", "max_pct": 0.50,
                                       "max_cash": 20.0}}  # loss 24.0 > 20
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(profile=profile, side="long", reference_price=4159.30,
                           atr=4.0, broker=BROKER_XAU, cost=COST_XAU)
    assert exc.value.reason_code == RISK_LIMIT_EXCEEDED


def test_insufficient_volume_rejects():
    profile = {**XAU_PROFILE, "volume": {"total": 0.005}}
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(profile=profile, side="long", reference_price=4159.30,
                           atr=4.0, broker=BROKER_XAU, cost=COST_XAU)
    assert exc.value.reason_code == INSUFFICIENT_VOLUME_FOR_THREE_LEGS


def test_tick_alignment_helpers():
    assert align_to_tick(4163.37, 0.01) == 4163.37
    assert align_to_tick(4163.375, 0.05) == 4163.4   # round-half-even on tick grid
    assert is_tick_aligned(4163.30, 0.01) is True
    assert is_tick_aligned(4163.305, 0.01) is False
    assert terminal_points(4.30, 0.01) == 430.0


def test_never_silently_stretches_levels():
    # The forbidden behavior: TP/SL stretched to arbitrary 20/50/100 units.
    # Here TP1 distance (min-clamped 3.0) is inside the cost envelope ->
    # explicit rejection, never a stretch.
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(profile=XAU_PROFILE, side="long", reference_price=4159.30,
                           atr=0.1, broker=BROKER_XAU,
                           cost=CostSnapshot(round_trip_cost_price=2.5,
                                             safety_buffer_price=0.5))
    assert exc.value.reason_code == TP1_TOO_CLOSE_TO_COST


def test_gross_r():
    r = calculate_gross_r("long", 4159.30, 4140.30,
                          [type("T", (), {"price": 4163.60, "allocation": 1 / 3})(),
                           type("T", (), {"price": 4167.70, "allocation": 1 / 3})(),
                           type("T", (), {"price": 4171.20, "allocation": 1 / 3})()])
    assert r == pytest.approx(0.432, abs=0.002)


def test_build_group_from_signal_full_parity():
    signal = {
        "bias": "long", "confidence": 0.71, "regime": "trend_up",
        "session": "london", "atr": 4.0,
        "entry_zone": [4159.10, 4159.50],
        "signal_id": "SGL-1", "model_version": "v3", "model_hash": "m" * 64,
        "config_hash": "c" * 64, "strategy_version": "s3",
        "expires_at_utc_ms": 1_900_000_000_000,
    }
    spec = build_trade_group_from_signal(
        signal, cfg=CFG, asset_key="XAUUSD", profile_id="xau_m15_intraday_v1",
        broker=BROKER_XAU, cost=COST_XAU, mode="paper",
        now_ms=1_700_000_000_000,
    )
    assert spec.side == "long"
    assert spec.entry.reference == 4159.30
    assert spec.geometry.tp1 == 4163.30
    assert spec.risk.total_volume == 0.06
    assert spec.mode == "paper"
    assert spec.profile_id == "xau_m15_intraday_v1"
    assert spec.expires_at_utc_ms == 1_900_000_000_000
    payload = spec.as_geometry_payload()
    assert payload["tp1"] == spec.geometry.tp1 == 4163.30


def test_build_group_from_signal_btc_candidate_blocked():
    signal = {"bias": "long", "atr": 5.0, "entry_zone": [60000.0, 60010.0],
              "expires_at_utc_ms": 1_900_000_000_000}
    with pytest.raises(GeometryRejected) as exc:
        build_trade_group_from_signal(
            signal, cfg=CFG, asset_key="BTCUSD", profile_id="btc_m5_scalp_v1",
            broker=BrokerSnapshot(symbol_point=0.01, tick_size=0.01, digits=2,
                                  spread=5.0, contract_size=1.0,
                                  volume_step=0.001, volume_min=0.001,
                                  balance=10000.0),
            cost=CostSnapshot(round_trip_cost_price=8.0, safety_buffer_price=2.0),
            mode="paper", now_ms=1_700_000_000_000,
        )
    assert exc.value.reason_code == PROFILE_NOT_VALIDATED


def test_build_group_from_signal_expired():
    signal = {"bias": "long", "atr": 4.0, "entry_zone": [4159.10, 4159.50],
              "expires_at_utc_ms": 1_500_000_000_000}
    with pytest.raises(GeometryRejected) as exc:
        build_trade_group_from_signal(
            signal, cfg=CFG, asset_key="XAUUSD", profile_id="xau_m15_intraday_v1",
            broker=BROKER_XAU, cost=COST_XAU, mode="paper",
            now_ms=1_700_000_000_000,
        )
    assert exc.value.reason_code == SIGNAL_EXPIRED


def test_invalid_tick_alignment_code_exists():
    # align-then-verify is deterministic, so INVALID_TICK_ALIGNMENT is defensive;
    # the code is part of the public reason-code contract.
    assert INVALID_TICK_ALIGNMENT == "INVALID_TICK_ALIGNMENT"
    assert not is_tick_aligned(1.005, 0.01)
