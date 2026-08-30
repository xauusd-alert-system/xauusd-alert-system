"""Property-based tests for position sizing (ТЗ §3.3)."""
from hypothesis import assume, given, strategies as st

from usstocks.sizing import size_position


@given(
    entry=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    stop=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    risk_budget=st.floats(min_value=1.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    max_notional=st.floats(min_value=100.0, max_value=10000.0, allow_nan=False, allow_infinity=False),
)
def test_sizing_always_within_budget(entry, stop, risk_budget, max_notional):
    """Actual risk should never exceed budget."""
    assume(abs(entry - stop) > 0.01)
    assume(entry > 0 and stop > 0)
    result = size_position(entry, stop, risk_per_trade_usd=risk_budget, max_notional_usd=max_notional)
    if result.ok:
        assert result.actual_risk_usd <= risk_budget + 1e-9
        assert result.notional_usd <= max_notional + 1e-9
        assert result.shares > 0


@given(
    entry=st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    stop_pct=st.floats(min_value=0.5, max_value=5.0, allow_nan=False, allow_infinity=False),
)
def test_sizing_respects_stop_first(entry, stop_pct):
    """Sizing should be stop-first, not notional-first."""
    import math
    stop = entry * (1 - stop_pct / 100.0)
    risk_budget = 10.0
    max_notional = 5000.0
    result = size_position(entry, stop, risk_per_trade_usd=risk_budget, max_notional_usd=max_notional)
    if result.ok:
        risk_per_share = abs(entry - stop)
        # Mirror sizing.py floor with epsilon to avoid FP off-by-one
        expected_shares_by_risk = math.floor((risk_budget + 1e-9) / risk_per_share)
        expected_shares_by_notional = math.floor((max_notional + 1e-9) / entry)
        assert result.shares == min(expected_shares_by_risk, expected_shares_by_notional)


@given(
    commission_per_share=st.floats(min_value=0.0, max_value=0.10, allow_nan=False, allow_infinity=False),
    fixed_commission=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)
def test_sizing_accounts_for_commission(commission_per_share, fixed_commission):
    """Sizing should account for round-trip commission."""
    entry = 100.0
    stop = 98.0
    risk_budget = 20.0
    result = size_position(
        entry, stop,
        risk_per_trade_usd=risk_budget,
        commission_per_share=commission_per_share,
        fixed_commission=fixed_commission,
    )
    if result.ok:
        expected_risk = (result.shares * abs(entry - stop) + result.estimated_commission_usd)
        assert abs(result.actual_risk_usd - expected_risk) < 1e-6
