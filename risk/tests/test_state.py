"""Tests for risk/state.py — persistent risk state (ТЗ 8.5, P1-7).

Covers:
  - save/load round-trip of all fields;
  - backwards compatibility with pre-P0-5 files (no starting_balance_today)
    and pre-P1-7 files (no hwm);
  - HWM ratchet (never decreases);
  - atomic save (tmp + replace leaves a single file).
"""

import json
from datetime import UTC, datetime

from risk.state import RiskState


def test_save_load_round_trip(tmp_path):
    path = str(tmp_path / "risk_state.json")
    st = RiskState(path)
    st.current_day = datetime.now(UTC).date()
    st.starting_equity_today = 10_000.0
    st.starting_balance_today = 9_950.0
    st.hwm = 10_200.0
    st.record_trade("XAUUSD")
    st.record_trade("XAUUSD")
    st.circuit_breaker_tripped = True
    st.save()

    st2 = RiskState(path)
    assert st2.current_day == st.current_day
    assert st2.starting_equity_today == 10_000.0
    assert st2.starting_balance_today == 9_950.0
    assert st2.hwm == 10_200.0
    assert st2.daily_trades_count == {"XAUUSD": 2}
    assert st2.circuit_breaker_tripped is True


def test_legacy_file_without_balance_and_hwm(tmp_path):
    """Pre-P0-5 / pre-P1-7 file: no starting_balance_today, no hwm."""
    path = str(tmp_path / "risk_state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "current_day": datetime.now(UTC).date().isoformat(),
                "starting_equity_today": 1000.0,
                "daily_trades_count": {"EURUSD": 1},
                "circuit_breaker_tripped": False,
            },
            f,
        )
    st = RiskState(path)
    # P0-5 fallback: balance anchor = stored equity.
    assert st.starting_balance_today == 1000.0
    # P1-7: hwm absent -> None until first update.
    assert st.hwm is None
    assert st.daily_trades_count == {"EURUSD": 1}


def test_hwm_ratchet_never_decreases(tmp_path):
    st = RiskState(str(tmp_path / "risk_state.json"))
    assert st.update_hwm(10_000.0) == 10_000.0
    assert st.update_hwm(9_000.0) == 10_000.0  # drawdown: no ratchet down
    assert st.update_hwm(10_500.0) == 10_500.0  # new high


def test_malformed_file_starts_fresh(tmp_path):
    path = str(tmp_path / "risk_state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    st = RiskState(path)  # must not raise
    assert st.current_day is None
    assert st.daily_trades_count == {}


def test_reset_for_new_day_anchors_balances(tmp_path):
    st = RiskState(str(tmp_path / "risk_state.json"))
    st.record_trade("XAUUSD")
    st.circuit_breaker_tripped = True
    st.reset_for_new_day(current_equity=5_000.0, current_balance=4_900.0)
    assert st.starting_equity_today == 5_000.0
    assert st.starting_balance_today == 4_900.0
    assert st.daily_trades_count == {}
    assert st.circuit_breaker_tripped is False
    assert st.is_today()

    # Legacy callers without balance: anchor falls back to equity (P0-5).
    st.reset_for_new_day(current_equity=6_000.0)
    assert st.starting_balance_today == 6_000.0


def test_atomic_save_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "risk_state.json")
    st = RiskState(path)
    st.save()
    assert (tmp_path / "risk_state.json").exists()
    assert not (tmp_path / "risk_state.json.tmp").exists()
