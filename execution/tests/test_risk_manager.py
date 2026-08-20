"""Tests for the audit fixes in execution/risk_manager.py.

Covers:
  - W8: concurrency / daily-trade limits read from config `execution.*`.
  - W9: the concurrent-position check counts only THIS system's positions
        (filtered by magic), not foreign/manual MT5 positions.
  - W10: daily circuit-breaker state is persisted and restored on restart.
"""
import json
import types

import pytest

import execution.risk_manager as rm


class _FakeAccount:
    def __init__(self, equity):
        self.equity = equity


class _FakePos:
    def __init__(self, magic):
        self.magic = magic


def _cfg(**exec_overrides):
    exec_cfg = {
        "max_concurrent_positions_global": 2,
        "max_daily_trades_per_asset": 5,
    }
    exec_cfg.update(exec_overrides)
    return {
        "execution": exec_cfg,
        "backtest": {"max_daily_loss_pct": 5.0},
    }


def _make_mt5(positions=None, equity=1000.0):
    fake = types.SimpleNamespace(
        _positions=positions or [],
        _equity=equity,
    )
    fake.initialize = lambda: True
    fake.account_info = lambda: _FakeAccount(fake._equity)
    fake.positions_get = lambda *a, **k: fake._positions or None
    return fake


@pytest.fixture()
def patched_mt5(monkeypatch):
    """Point risk_manager's mt5 at a controllable fake."""
    fake = _make_mt5()
    monkeypatch.setattr(rm.mt5, "initialize", fake.initialize)
    monkeypatch.setattr(rm.mt5, "account_info", fake.account_info)
    monkeypatch.setattr(rm.mt5, "positions_get", fake.positions_get)
    yield fake


@pytest.fixture()
def state_path(tmp_path):
    """Isolate each test's persisted risk state."""
    return str(tmp_path / "risk_state.json")


def test_limits_read_from_config(patched_mt5, state_path):
    """W8: max_concurrent_positions_global and max_daily_trades_per_asset are
    read from config instead of the hard-coded 3/10."""
    mgr = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state_path)
    assert mgr.max_concurrent_positions == 2
    assert mgr.max_daily_trades_per_asset == 5
    assert mgr.max_open_positions_per_asset == 2


def test_group_aware_counting_three_legs_consume_one_slot(patched_mt5, state_path):
    """Audit 2026-08-19: a 3-leg group consumes ONE budget slot, so the 2-slot
    budget allows a second ASSET's group even though 3+ positions are open."""
    mgr = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state_path)

    # XAUUSD group open (3 legs, one group_key) -> another asset still fits.
    ok, _ = mgr.can_trade(
        "XAGUSD",
        groups_by_asset={"XAUUSD": {"G1"}},
        singles_by_asset={},
    )
    assert ok is True

    # Two groups open -> the 2-slot budget is full.
    ok2, reason2 = mgr.can_trade(
        "BTCUSD",
        groups_by_asset={"XAUUSD": {"G1"}, "XAGUSD": {"G2"}},
        singles_by_asset={},
    )
    assert ok2 is False
    assert "groups limit" in reason2


def test_per_asset_group_cap_enforced(patched_mt5, state_path):
    """Audit 2026-08-19: max_open_positions_per_asset (was declared but never
    wired) now bounds groups per asset; other assets stay free."""
    mgr = rm.InstitutionalRiskManager(
        _cfg(max_concurrent_positions_global=6), magic=777111, state_path=state_path)

    ok, reason = mgr.can_trade(
        "XAUUSD",
        groups_by_asset={"XAUUSD": {"G1", "G2"}},
        singles_by_asset={},
    )
    assert ok is False
    assert "Max open groups for XAUUSD" in reason

    ok2, _ = mgr.can_trade(
        "BTCUSD",
        groups_by_asset={"XAUUSD": {"G1", "G2"}},
        singles_by_asset={},
    )
    assert ok2 is True


def test_unknown_tickets_count_as_single_positions(patched_mt5, state_path):
    """Positions unknown to active_trades (restart edge) consume one slot each
    — conservative, they cannot hide inside a group."""
    mgr = rm.InstitutionalRiskManager(
        _cfg(max_concurrent_positions_global=6), magic=777111, state_path=state_path)

    ok, _ = mgr.can_trade(
        "XAUUSD",
        groups_by_asset={},
        singles_by_asset={"XAUUSD": 1},
    )
    assert ok is True

    ok2, reason2 = mgr.can_trade(
        "XAUUSD",
        groups_by_asset={},
        singles_by_asset={"XAUUSD": 2},
    )
    assert ok2 is False
    assert "Max open groups for XAUUSD" in reason2


def test_legacy_can_trade_without_group_info_keeps_old_behavior(patched_mt5, state_path):
    """Callers/tests that pass no group info keep the raw per-position count."""
    patched_mt5._positions = [_FakePos(777111), _FakePos(777111)]
    mgr = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state_path)
    ok, reason = mgr.can_trade("XAUUSD")
    assert ok is False
    assert "concurrent positions limit" in reason


def test_concurrency_counts_only_own_magic(patched_mt5, state_path):
    """W9: a foreign/manual position (different magic) must not consume our
    concurrency budget."""
    # 2 foreign positions open (no magic match) -> we should still be allowed.
    patched_mt5._positions = [_FakePos(1), _FakePos(2)]
    mgr = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state_path)
    ok, _ = mgr.can_trade("XAUUSD")
    assert ok is True

    # 2 own positions open -> blocked by the concurrency limit.
    patched_mt5._positions = [_FakePos(777111), _FakePos(777111)]
    mgr2 = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state_path)
    ok2, reason2 = mgr2.can_trade("XAUUSD")
    assert ok2 is False
    assert "concurrent" in reason2.lower()


def test_daily_trade_limit_from_config(patched_mt5, state_path):
    """W8: after max_daily_trades_per_asset executions, trading is blocked."""
    mgr = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state_path)
    mgr.can_trade("XAUUSD")  # establishes the day budget (starting equity)
    for _ in range(5):
        mgr.record_trade_executed("XAUUSD")
    ok, reason = mgr.can_trade("XAUUSD")
    assert ok is False
    assert "Daily trade limit" in reason


def test_state_persisted_and_restored(patched_mt5, tmp_path):
    """W10: daily counters and the circuit-breaker budget survive a restart."""
    state = str(tmp_path / "risk_state.json")
    mgr = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state)
    mgr.can_trade("EURUSD")  # establishes the day's starting-equity budget
    for _ in range(2):
        mgr.record_trade_executed("EURUSD")

    assert json.load(open(state))["daily_trades_count"]["EURUSD"] == 2

    # A fresh instance restores the persisted counter (same day).
    mgr2 = rm.InstitutionalRiskManager(_cfg(), magic=777111, state_path=state)
    ok, reason = mgr2.can_trade("EURUSD")  # equity unchanged -> same day budget
    assert "Daily trade limit" not in reason
    # It restored the counter -> the 3rd trade is allowed, the 5th cap holds.
    for _ in range(3):
        mgr2.record_trade_executed("EURUSD")
    ok2, reason2 = mgr2.can_trade("EURUSD")
    assert ok2 is False
    assert "Daily trade limit" in reason2
