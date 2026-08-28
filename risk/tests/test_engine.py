"""Tests for risk/engine.py — RiskEngine aggregation (ТЗ 8.5).

Covers:
  - each gate can block independently (test_can_open_aggregates_all_gates);
  - circuit breaker blocks until the next day;
  - P1-7 drawdown throttle: HWM is crossing/persistent, throttle recovers
    only on new equity highs;
  - HWM persisted and updated through RiskState;
  - P2-10 guard: the rate throttle knows nothing about daily limits.
"""

import json
from datetime import timedelta

import pytest

import risk.engine as engine_mod
from risk.engine import RiskEngine
from risk.state import RiskState
from risk.throttle import RateThrottle


def _cfg(**exec_overrides):
    exec_cfg = {
        "max_concurrent_positions_global": 2,
        "max_daily_trades_per_asset": 5,
        "max_open_positions_per_asset": 2,
    }
    exec_cfg.update(exec_overrides)
    return {
        "execution": exec_cfg,
        "backtest": {"max_daily_loss_pct": 5.0},
        "risk": {"circuit_breaker": {"exclude_swaps": True}},
    }


@pytest.fixture()
def state_path(tmp_path):
    return str(tmp_path / "risk_state.json")


def _engine(cfg=None, state_path="unused.json", **kw):
    cfg = cfg or _cfg()
    return RiskEngine(cfg, magic=777111, state_path=state_path, legacy_throttle=False, **kw)


# ==========================================================================
# Aggregation: each gate can block
# ==========================================================================


def test_can_open_aggregates_all_gates(tmp_path):
    """Each gate, in isolation, is able to block; otherwise can_open passes.
    Every gate uses its own state file so breaker trips do not leak."""
    state_path = str(tmp_path / "risk_state.json")
    eng = _engine(state_path=state_path)

    # Baseline: healthy account passes all gates.
    ok, reason = eng.can_open("XAUUSD", equity=10_000.0, balance=10_000.0, use_legacy_throttle=False)
    assert ok, reason

    # Gate 1: circuit breaker (daily loss beyond 5% of 10_000 = 500).
    ok, reason = eng.can_open("XAUUSD", equity=9_400.0, balance=9_400.0, use_legacy_throttle=False)
    assert not ok
    assert "CIRCUIT BREAKER" in reason

    # Gate 2: concurrency (2 own groups open, global cap 2).
    eng2 = _engine(state_path=str(tmp_path / "s2.json"))
    ok, reason = eng2.can_open(
        "XAUUSD",
        equity=10_000.0,
        groups_by_asset={"XAGUSD": {"G1"}, "BTCUSD": {"G2"}},
        singles_by_asset={},
        use_legacy_throttle=False,
    )
    assert not ok
    assert "groups limit" in reason

    # Gate 3: daily trades per asset (counter filled to the cap of 5).
    eng3 = _engine(state_path=str(tmp_path / "s3.json"))
    eng3.can_open("XAUUSD", equity=10_000.0, use_legacy_throttle=False)
    for _ in range(5):
        eng3.record_trade("XAUUSD")
    ok, reason = eng3.can_open("XAUUSD", equity=10_000.0, use_legacy_throttle=False)
    assert not ok
    assert "Daily trade limit" in reason

    # Gate 4: rate throttle (2 orders/min cap, window full).
    eng4 = _engine(state_path=str(tmp_path / "s4.json"), rate_throttle=RateThrottle(max_orders_per_minute=2))
    eng4.rate_throttle.record_order("XAUUSD")
    eng4.rate_throttle.record_order("XAUUSD")
    ok, reason = eng4.can_open("XAUUSD", equity=10_000.0, use_legacy_throttle=False)
    assert not ok
    assert "rate_throttled" in reason

    # Gate 5: drawdown throttle vs persistent HWM (-9% -> no entries).
    eng5 = _engine(state_path=str(tmp_path / "s5.json"))
    eng5.state.hwm = 10_000.0
    ok, reason = eng5.can_open("XAUUSD", equity=9_100.0, balance=9_100.0, use_legacy_throttle=False)
    assert not ok
    assert "drawdown_throttle" in reason

    # Gate 6: cluster exposure caps (P1-4 required caps).
    eng6 = _engine(state_path=str(tmp_path / "s6.json"))
    ok, reason = eng6.can_open(
        "XAUUSD",
        equity=10_000.0,
        cluster="metals",
        add_risk_pct=0.005,
        current_risk_by_cluster={"metals": 0.0},
        cluster_cap=0.004,
        total_cap=0.0075,
        use_legacy_throttle=False,
    )
    assert not ok
    assert "cluster_exposure_exceeded" in reason

    # Gate 6b: missing caps raise (P1-4 — no silent defaults).
    with pytest.raises(TypeError):
        eng6.can_open("XAUUSD", equity=10_000.0, cluster="metals", add_risk_pct=0.001, use_legacy_throttle=False)


# ==========================================================================
# Circuit breaker until next day
# ==========================================================================


def test_circuit_breaker_blocks_until_next_day(state_path):
    """Once tripped, the breaker stays blocking for the rest of the UTC day
    (even if equity recovers), and resets on the next day."""
    eng = _engine(state_path=state_path)
    assert eng.can_open("XAUUSD", equity=10_000.0, balance=10_000.0, use_legacy_throttle=False)

    # Trip it: -6% > 5% limit.
    ok, reason = eng.can_open("XAUUSD", equity=9_400.0, balance=9_400.0, use_legacy_throttle=False)
    assert not ok
    assert "CIRCUIT BREAKER" in reason
    assert eng.state.circuit_breaker_tripped is True

    # Equity recovers intraday -> still blocked (persistent trip flag).
    ok, reason = eng.can_open("XAUUSD", equity=11_000.0, balance=11_000.0, use_legacy_throttle=False)
    assert not ok
    assert "Circuit Breaker" in reason

    # Next UTC day -> budget re-anchored, breaker re-armed.
    eng.state.current_day = engine_mod.datetime.now(engine_mod.timezone.utc).date() - timedelta(days=1)
    ok, reason = eng.can_open("XAUUSD", equity=11_000.0, balance=11_000.0, use_legacy_throttle=False)
    assert ok, reason
    assert eng.state.circuit_breaker_tripped is False
    assert eng.state.starting_equity_today == 11_000.0


# ==========================================================================
# P1-7: drawdown throttle vs crossing HWM
# ==========================================================================


def test_drawdown_throttle_recovers(state_path):
    """P1-7 semantics: the throttle follows the persistent HWM, NOT the daily
    anchor. It engages at -8% from HWM and recovers ONLY when equity makes a
    new high — a new UTC day alone does NOT re-arm it."""
    eng = _engine(state_path=state_path)
    eng.state.hwm = 10_000.0

    # -9% from HWM -> blocked.
    ok, reason = eng.can_open("XAUUSD", equity=9_100.0, balance=9_100.0, use_legacy_throttle=False)
    assert not ok
    assert "drawdown_throttle" in reason

    # Partial recovery to -5% -> unblocked (above the -8% no-entry level)
    # even though it is still a deep drawdown from HWM.
    ok, reason = eng.can_open("XAUUSD", equity=9_500.0, balance=9_500.0, use_legacy_throttle=False)
    assert ok, reason

    # New equity high -> HWM ratchets up; a subsequent drop measured against
    # the NEW HWM.
    ok, _ = eng.can_open("XAUUSD", equity=10_500.0, balance=10_500.0, use_legacy_throttle=False)
    assert eng.state.hwm == 10_500.0
    ok, reason = eng.can_open("XAUUSD", equity=10_500.0 * 0.91, balance=10_500.0 * 0.91, use_legacy_throttle=False)
    assert not ok
    assert "drawdown_throttle" in reason

    # Calendar day change alone does NOT clear the throttle (HWM crossing).
    eng.state.current_day = engine_mod.datetime.now(engine_mod.timezone.utc).date() - timedelta(days=1)
    # re-anchor happens inside can_open; equity still -9% vs HWM
    ok, reason = eng.can_open("XAUUSD", equity=10_500.0 * 0.91, balance=10_500.0 * 0.91, use_legacy_throttle=False)
    assert not ok
    assert "drawdown_throttle" in reason


def test_hwm_persisted_and_updated(state_path, tmp_path):
    """P1-7: the HWM lives in risk_state.json, ratchets upward only, and
    survives a restart."""
    eng = _engine(state_path=state_path)
    # equity 10_000 -> HWM established at 10_000.
    ok, _ = eng.can_open("XAUUSD", equity=10_000.0, balance=10_000.0, use_legacy_throttle=False)
    assert eng.state.hwm == 10_000.0

    # Drawdown to 9_800 -> HWM NOT lowered.
    eng.can_open("XAUUSD", equity=9_800.0, balance=9_800.0, use_legacy_throttle=False)
    assert eng.state.hwm == 10_000.0

    # New high -> ratchet.
    eng.can_open("XAUUSD", equity=10_200.0, balance=10_200.0, use_legacy_throttle=False)
    assert eng.state.hwm == 10_200.0

    # Persisted to disk.
    data = json.load(open(state_path))
    assert data["hwm"] == 10_200.0

    # Restart: fresh engine loads the same HWM. A looser daily-loss limit
    # (12%) keeps the circuit breaker out of the way so the P1-7 gate is
    # tested in isolation.
    cfg_loose = _cfg()
    cfg_loose["backtest"]["max_daily_loss_pct"] = 12.0
    eng2 = _engine(cfg=cfg_loose, state_path=state_path)
    assert eng2.state.hwm == 10_200.0
    # -9% vs the restored HWM -> drawdown throttle blocks immediately.
    ok, reason = eng2.can_open("XAUUSD", equity=10_200.0 * 0.91, balance=10_200.0 * 0.91, use_legacy_throttle=False)
    assert not ok
    assert "drawdown_throttle" in reason


# ==========================================================================
# P2-10 guard: throttle has no daily limits
# ==========================================================================


def test_no_daily_limits_in_throttle():
    """P2-10: the rate throttle must know NOTHING about daily limits —
    one source of daily limits is risk/limits.py."""
    rt = RateThrottle(max_orders_per_minute=2)
    for forbidden in (
        "max_trades_per_day",
        "daily_trades_count",
        "trades_today",
        "max_daily_loss_pct",
        "on_trade_closed",
        "check_daily_trades",
    ):
        assert not hasattr(rt, forbidden), forbidden
    # class-level guard (no such attrs even as class attributes)
    import inspect

    source = inspect.getsource(type(rt))
    assert "max_trades_per_day" not in source
    assert "trades_today" not in source
    assert "daily_loss" not in source.lower()
    assert "daily_trades" not in source.lower()


def test_engine_aliases_and_summary(state_path):
    """can_trade is a backwards-compatible alias of can_open; summary()
    exposes the aggregated state."""
    eng = _engine(state_path=state_path)
    ok_alias, _ = eng.can_trade("XAUUSD", equity=10_000.0, balance=10_000.0, use_legacy_throttle=False)
    ok_open, _ = eng.can_open("XAUUSD", equity=10_000.0, balance=10_000.0, use_legacy_throttle=False)
    assert ok_alias and ok_open

    snap = eng.summary()
    assert snap["hwm"] == 10_000.0
    assert snap["circuit_breaker_tripped"] is False
    assert "XAUUSD" in snap["rate_orders_per_asset"]


def test_engine_state_file_backwards_compat(state_path):
    """A pre-P1-7 risk_state.json (no 'hwm' key) loads cleanly and the engine
    establishes the HWM on the first equity observation."""
    st = RiskState(state_path)
    st.save()
    eng = _engine(state_path=state_path)
    assert eng.state.hwm is None
    ok, _ = eng.can_open("XAUUSD", equity=5_000.0, balance=5_000.0, use_legacy_throttle=False)
    assert ok
    assert eng.state.hwm == 5_000.0
