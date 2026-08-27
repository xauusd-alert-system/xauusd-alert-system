"""Tests for Position Sizing with commission and fee accounting (P1-5)."""
import pytest

from usstocks.sizing import size_position


def test_sizing_without_commission_matches_baseline():
    r = size_position(entry=100.0, stop=99.5, risk_per_trade_usd=10.0, max_notional_usd=5000.0)
    assert r.ok
    assert r.shares == 20  # floor(10.0 / 0.5)
    assert r.actual_risk_usd == 10.0
    assert r.estimated_commission_usd == 0.0


def test_sizing_deducts_per_share_commission():
    # Stop distance is $0.48, roundtrip commission is 2 * $0.01 = $0.02
    # Total risk per share is $0.50 -> exactly 20 shares for $10 risk
    r = size_position(
        entry=100.0,
        stop=99.52,
        risk_per_trade_usd=10.0,
        max_notional_usd=5000.0,
        commission_per_share=0.01,
    )
    assert r.ok
    assert r.shares == 20
    assert r.actual_risk_usd <= 10.0 + 1e-9
    assert r.estimated_commission_usd == pytest.approx(0.40)


def test_sizing_fixed_commission_cap():
    # Fixed commission $6 (rt = $12) exceeds $10 risk budget
    r = size_position(
        entry=100.0,
        stop=99.0,
        risk_per_trade_usd=10.0,
        fixed_commission=6.0,
    )
    assert not r.ok
    assert r.reason == "COMMISSION_EXCEEDS_RISK_BUDGET"
