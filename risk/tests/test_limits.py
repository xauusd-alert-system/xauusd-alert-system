"""Tests for risk/limits.py — daily limits + circuit breaker (ТЗ 8.5).

Key scenarios duplicated from execution/tests/test_risk_manager.py against
the NEW package (the old tests keep testing the shim). Covers P0-5
(swap-excluding breaker), W8/W9 (config limits, magic filter), W10
(persistence), P2-10 (single daily-limit source).
"""

import json
import types

import pytest

import risk.limits as limits_mod
from risk.limits import RiskLimits
from risk.state import RiskState


class _FakeAccount:
    def __init__(self, equity, balance=None):
        self.equity = equity
        self.balance = equity if balance is None else balance


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


def _make_mt5(positions=None, equity=1000.0, balance=None):
    fake = types.SimpleNamespace(
        _positions=positions or [],
        _equity=equity,
        _balance=equity if balance is None else balance,
    )
    fake.initialize = lambda: True
    fake.account_info = lambda: _FakeAccount(fake._equity, fake._balance)
    fake.positions_get = lambda *a, **k: fake._positions or None
    return fake


@pytest.fixture()
def patched_mt5(monkeypatch):
    fake = _make_mt5()
    monkeypatch.setattr(limits_mod.mt5, "initialize", fake.initialize)
    monkeypatch.setattr(limits_mod.mt5, "account_info", fake.account_info)
    monkeypatch.setattr(limits_mod.mt5, "positions_get", fake.positions_get)
    yield fake


@pytest.fixture()
def state_path(tmp_path):
    return str(tmp_path / "risk_state.json")


def test_circuit_breaker_ignores_swaps(patched_mt5, state_path):
    """P0-5: a -$50 swap settling into balance must NOT trip the breaker
    (daily PnL = trading balance delta + floating, swap-free)."""
    lim = RiskLimits(_cfg(), magic=777111, state_path=state_path)
    ok, _ = lim.can_trade("XAUUSD")  # anchor day at 1000
    patched_mt5._balance = 950.0  # swap settles
    patched_mt5._equity = 980.0  # floating -30
    ok, reason = lim.can_trade("XAUUSD")
    assert ok, reason
    assert "CIRCUIT BREAKER" not in reason


def test_circuit_breaker_trips_on_trading_loss(patched_mt5, state_path):
    lim = RiskLimits(_cfg(), magic=777111, state_path=state_path)
    assert lim.can_trade("XAUUSD")
    patched_mt5._balance = 930.0
    patched_mt5._equity = 935.0
    ok, reason = lim.can_trade("XAUUSD")
    assert not ok
    assert "CIRCUIT BREAKER" in reason


def test_exclude_swaps_config_off(patched_mt5, state_path):
    """P0-5: exclude_swaps=false restores the legacy equity-delta rule."""
    cfg = _cfg()
    cfg["risk"] = {"circuit_breaker": {"exclude_swaps": False}}
    lim = RiskLimits(cfg, magic=777111, state_path=state_path)
    assert lim.can_trade("XAUUSD")
    patched_mt5._balance = 930.0
    patched_mt5._equity = 935.0
    ok, reason = lim.can_trade("XAUUSD")
    assert not ok
    assert "CIRCUIT BREAKER" in reason


def test_concurrency_counts_only_own_magic(patched_mt5, state_path):
    """W9: foreign/manual positions do not consume our budget."""
    patched_mt5._positions = [_FakePos(1), _FakePos(2)]
    lim = RiskLimits(_cfg(), magic=777111, state_path=state_path)
    ok, _ = lim.can_trade("XAUUSD")
    assert ok

    patched_mt5._positions = [_FakePos(777111), _FakePos(777111)]
    lim2 = RiskLimits(_cfg(), magic=777111, state_path=state_path)
    ok2, reason2 = lim2.can_trade("XAUUSD")
    assert not ok2
    assert "concurrent" in reason2.lower()


def test_group_aware_counting(patched_mt5, state_path):
    """A 3-leg group consumes ONE slot; the 2-slot budget allows a second
    asset's group."""
    lim = RiskLimits(_cfg(), magic=777111, state_path=state_path)
    ok, _ = lim.can_trade("XAGUSD", groups_by_asset={"XAUUSD": {"G1"}}, singles_by_asset={})
    assert ok
    ok2, reason2 = lim.can_trade("BTCUSD", groups_by_asset={"XAUUSD": {"G1"}, "XAGUSD": {"G2"}}, singles_by_asset={})
    assert not ok2
    assert "groups limit" in reason2


def test_daily_trade_limit_and_persistence(patched_mt5, tmp_path):
    """W8 + W10: the per-asset daily cap holds across a restart."""
    state = str(tmp_path / "risk_state.json")
    lim = RiskLimits(_cfg(), magic=777111, state_path=state)
    lim.can_trade("EURUSD")
    for _ in range(5):
        lim.record_trade_executed("EURUSD")

    lim2 = RiskLimits(_cfg(), magic=777111, state_path=state)
    ok, reason = lim2.can_trade("EURUSD")
    assert not ok
    assert "Daily trade limit" in reason
    data = json.load(open(state))
    assert data["daily_trades_count"]["EURUSD"] == 5


def test_daily_pnl_math_exclude_swaps(patched_mt5, state_path):
    """Direct unit check of the P0-5 formula:
    (balance - starting_balance) + (equity - balance), which collapses to
    `equity - starting_balance_today` and therefore differs from the legacy
    `equity - starting_equity_today` whenever the two anchors diverge.

    Anchor at equity 1000 / balance 990 (pre-existing floating +10 carried
    at anchor). Then balance 950 (swap -40) with equity 980:
    exclude_swaps PnL = (950-990) + (980-950) = -10;
    legacy equity rule = 980-1000 = -20."""
    lim = RiskLimits(_cfg(), magic=777111, state_path=state_path)
    lim.check_circuit_breaker(1000.0, 990.0)  # anchor
    assert lim.daily_pnl(980.0, 950.0) == pytest.approx(-10.0)
    assert lim.daily_pnl(1000.0, 990.0) == pytest.approx(10.0)

    lim2 = RiskLimits(
        {"backtest": {"max_daily_loss_pct": 5.0}, "risk": {"circuit_breaker": {"exclude_swaps": False}}},
        magic=777111,
        state=RiskState(state_path),
    )
    lim2.state.starting_equity_today = 1000.0
    # legacy rule: equity delta = -20
    assert lim2.daily_pnl(980.0, 950.0) == pytest.approx(-20.0)
