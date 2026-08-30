"""Property-based tests for risk engine (ТЗ §3.3)."""
from datetime import datetime, timedelta

from hypothesis import given, strategies as st

from usstocks.models import RiskState
from usstocks.risk_engine import RiskEngine


@given(
    realized_pnl=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    trades_taken=st.integers(min_value=0, max_value=10),
    consecutive_losses=st.integers(min_value=0, max_value=5),
)
def test_risk_engine_deterministic(realized_pnl, trades_taken, consecutive_losses):
    """Risk engine should be deterministic (same input = same output)."""
    engine = RiskEngine(
        personal_daily_stop_usd=-20.0,
        max_trades_per_day=2,
        max_consecutive_losses=2,
    )
    state = RiskState(
        session_date="2026-08-30",
        realized_pnl_usd=realized_pnl,
        trades_taken=trades_taken,
        consecutive_losses=consecutive_losses,
    )
    now = datetime.now().astimezone()
    session_close = now + timedelta(hours=2)
    decision1 = engine.evaluate(state, now, session_close)
    decision2 = engine.evaluate(state, now, session_close)
    assert decision1.allowed == decision2.allowed
    assert decision1.code == decision2.code
    assert decision1.reason == decision2.reason


@given(
    personal_stop=st.floats(min_value=-100.0, max_value=-10.0, allow_nan=False, allow_infinity=False),
    realized_pnl=st.floats(min_value=-200.0, max_value=100.0, allow_nan=False, allow_infinity=False),
)
def test_personal_daily_stop_triggers_correctly(personal_stop, realized_pnl):
    """Personal daily stop should trigger when realized PnL <= threshold."""
    engine = RiskEngine(personal_daily_stop_usd=personal_stop)
    state = RiskState(
        session_date="2026-08-30",
        realized_pnl_usd=realized_pnl,
    )
    now = datetime.now().astimezone()
    session_close = now + timedelta(hours=2)
    decision = engine.evaluate(state, now, session_close)
    if realized_pnl <= personal_stop:
        assert not decision.allowed
        assert decision.code == "PERSONAL_DAILY_STOP"
    else:
        if decision.code == "PERSONAL_DAILY_STOP":
            assert realized_pnl <= personal_stop
