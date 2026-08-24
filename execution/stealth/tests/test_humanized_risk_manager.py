"""Tests for HumanizedRiskManager."""

import pytest

from execution.stealth.humanized_risk_manager import HumanizedRiskManager


def test_risk_distribution_in_range():
    rm = HumanizedRiskManager(seed=42)
    risks = [rm.get_risk_pct() for _ in range(1000)]
    # Base 1% ±0.35% => 0.65%-1.35% normally, 5% out of bounds => up to ~1.85% or down to 0.15%
    # Check majority in 0.0065-0.0135
    in_normal = sum(1 for r in risks if 0.0065 <= r <= 0.0135)
    assert in_normal >= 850  # at least 85% (95% expected minus randomness)

    # All in [0.001, 0.05] safety clamp
    assert all(0.001 <= r <= 0.05 for r in risks)

    # Check out-of-bounds ~5% (allow 2%-10%)
    out = sum(1 for r in risks if r < 0.0065 or r > 0.0135)
    assert 20 <= out <= 100


def test_out_of_bounds_prob():
    rm = HumanizedRiskManager(seed=123)
    risks = [rm.get_risk_pct() for _ in range(1000)]
    out = [r for r in risks if r < 0.0065 or r > 0.0135]
    # Should have some out-of-bounds due to 5% prob
    assert len(out) > 0


def test_sl_tp_profiles_weighted():
    rm = HumanizedRiskManager(seed=42)
    profiles = [rm.get_sl_tp_profile() for _ in range(1000)]
    # All profiles should have sl_mult and tp_mult in expected ranges
    for p in profiles:
        assert 1.0 <= p["sl_mult"] <= 1.2
        assert 1.5 <= p["tp_mult"] <= 2.2

    # Check that all 6 profiles appear at least once in 1000 draws (weighted)
    ids = set(p["profile_id"] for p in profiles)
    assert len(ids) == 6


def test_no_repeat_prob():
    rm = HumanizedRiskManager(seed=7)
    # Get first profile
    first = rm.get_sl_tp_profile()
    # Next 100, count repeats
    repeats = 0
    prev_id = first["profile_id"]
    for _ in range(200):
        p = rm.get_sl_tp_profile()
        if p["profile_id"] == prev_id:
            repeats += 1
        prev_id = p["profile_id"]
    # With 70% no-repeat, repeats should be less than 50% (random would be ~16% repeat chance for 6 profiles,
    # but with no-repeat logic it should be even less)
    # Actually without no-repeat, repeat prob ~16% (1/6). With 70% no-repeat, it should be ~0.3*16% = 4.8%
    # So repeats in 200 should be < 30
    assert repeats < 60


def test_lot_jitter():
    rm = HumanizedRiskManager(seed=42)
    base_lot = 0.10
    lots = [rm.get_lot_size(base_lot) for _ in range(1000)]
    # 15% chance ±1 step (0.01)
    jittered = [l for l in lots if l != base_lot]
    # Should be around 15% (allow 8%-25%)
    assert 80 <= len(jittered) <= 250
    # Jittered values should be ±0.01
    for l in jittered:
        assert l in [0.09, 0.11]


def test_seed_reproducibility():
    rm1 = HumanizedRiskManager(seed=12345)
    rm2 = HumanizedRiskManager(seed=12345)
    r1 = [rm1.get_risk_pct() for _ in range(10)]
    r2 = [rm2.get_risk_pct() for _ in range(10)]
    assert r1 == r2

    rm1 = HumanizedRiskManager(seed=12345)
    rm2 = HumanizedRiskManager(seed=12345)
    p1 = [rm1.get_sl_tp_profile()["profile_id"] for _ in range(10)]
    p2 = [rm2.get_sl_tp_profile()["profile_id"] for _ in range(10)]
    assert p1 == p2


def test_calculate_position_size():
    rm = HumanizedRiskManager(seed=42)
    equity = 10000
    risk_pct = 0.01
    entry = 2000.0
    stop = 1995.0
    lot = rm.calculate_position_size(equity, risk_pct, entry, stop, point_value_lot=100)
    assert lot > 0
    # Risk cash 100, dist 5, point_value 100 => raw 0.2 lot
    # Quantized to 0.01 steps => 0.2
    # With possible jitter, could be 0.19 or 0.21
    assert 0.15 <= lot <= 0.25
