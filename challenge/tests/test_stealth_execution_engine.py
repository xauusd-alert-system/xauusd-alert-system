"""Tests for StealthExecutionEngine — gates, process_signal, manage_position."""
import pytest
from datetime import datetime, timedelta, timezone

from challenge.stealth.execution_engine import StealthExecutionEngine
from challenge.stealth.humanized_timer import HumanizedTimer
from challenge.stealth.humanized_risk_manager import HumanizedRiskManager
from challenge.stealth.session_simulator import SessionSimulator
from challenge.stealth.browser_humanizer import BrowserHumanizer
from challenge.stealth.equity_curve_humanizer import EquityCurveHumanizer
from challenge.orb_strategy import ORBSignal

ET = timezone(timedelta(hours=-4))

CFG = {
    "challenge": {
        "platform": {"url": "https://test.com", "session_id": "123"},
        "session": {
            "start_local": "18:30",
            "end_local": "00:55",
            "flatten_local": "00:45",
            "min_trading_days": 5,
        },
        "risk": {
            "per_trade_risk_usd": 5,
            "daily_loss_stop": 25,
            "total_loss_stop": 60,
            "daily_profit_lock": 20,
            "max_open_positions": 2,
            "max_leverage": 5,
            "stop_pct": 0.005,
            "tp_ratio": 1.5,
        },
        "browser": {"headless": False},
    },
    "stealth": {
        "risk_base_pct": 0.01,
    },
}


def _make_engine(seed=42):
    """Create a deterministic engine for testing."""
    timer = HumanizedTimer(seed=seed)
    risk = HumanizedRiskManager(
        start_balance=1000.0, risk_base_pct=0.01, seed=seed,
    )
    session = SessionSimulator(seed=seed, cfg={"skip_day_chance": 0.0})
    humanizer = BrowserHumanizer(seed=seed)
    equity_humanizer = EquityCurveHumanizer(seed=seed)
    return StealthExecutionEngine(
        CFG,
        timer=timer,
        risk=risk,
        session=session,
        humanizer=humanizer,
        equity_humanizer=equity_humanizer,
        seed=seed,
    )


def _signal(symbol="TSLA", bias="long", entry=250.0, stop=248.0, tp=254.0):
    return ORBSignal(
        symbol=symbol, bias=bias, entry=entry, stop=stop, tp=tp,
        range_high=250.0, range_low=248.0, range_pct=0.8,
        volume_ratio=1.5, gap_pct=1.0,
    )


class TestProcessSignal:
    def test_returns_plan_for_valid_signal(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        # Ensure session is active
        engine.session._today = now_et.date()
        engine.session._skip_today = False
        engine.session._trades_today = 0

        plan = engine.process_signal(
            _signal(), now_et,
            equity=1000.0, floating_pnl=0, daily_pnl=0, overall_pnl=0,
        )
        assert plan is not None
        assert plan["symbol"] == "TSLA"
        assert plan["bias"] == "long"
        assert plan["shares"] >= 1
        assert plan["delay"] > 0

    def test_returns_none_on_session_inactive(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 29, 10, 0)  # Saturday
        plan = engine.process_signal(
            _signal(), now_et,
            equity=1000.0, floating_pnl=0, daily_pnl=0, overall_pnl=0,
        )
        assert plan is None

    def test_returns_none_on_daily_limit(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        engine.session._today = now_et.date()
        engine.session._skip_today = False
        plan = engine.process_signal(
            _signal(), now_et,
            equity=1000.0, floating_pnl=-31.0, daily_pnl=-31.0, overall_pnl=-31.0,
        )
        assert plan is None

    def test_returns_none_on_overall_buffer(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        engine.session._today = now_et.date()
        engine.session._skip_today = False
        plan = engine.process_signal(
            _signal(), now_et,
            equity=905.0, floating_pnl=-50.0, daily_pnl=-50.0, overall_pnl=-91.0,
        )
        assert plan is None

    def test_returns_none_near_session_end(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 28)  # 2 min before 10:30
        engine.session._today = now_et.date()
        engine.session._skip_today = False
        plan = engine.process_signal(
            _signal(), now_et,
            equity=1000.0, floating_pnl=0, daily_pnl=0, overall_pnl=0,
        )
        assert plan is None

    def test_plan_has_method(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        engine.session._today = now_et.date()
        engine.session._skip_today = False
        plan = engine.process_signal(
            _signal(), now_et,
            equity=1000.0, floating_pnl=0, daily_pnl=0, overall_pnl=0,
        )
        assert plan is not None
        assert "method" in plan
        assert plan["method"]["type"] in ("hotkey", "dom_click")


class TestManagePosition:
    def test_manage_returns_list(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        position = {
            "symbol": "TSLA", "side": "long", "qty": 10,
            "entry": 100.0, "stop": 99.0, "tp": 106.0,
            "remaining_shares": 10, "already_partialed": False,
            "current_price": 102.0,
        }
        actions = engine.manage_position(position, now_et, floating_pnl=20.0)
        assert isinstance(actions, list)

    def test_manage_with_zero_risk(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        position = {
            "symbol": "TSLA", "side": "long", "qty": 10,
            "entry": 100.0, "stop": 100.0, "tp": 106.0,  # zero risk
            "remaining_shares": 10, "already_partialed": False,
            "current_price": 100.0,
        }
        actions = engine.manage_position(position, now_et, floating_pnl=0)
        assert actions == []


class TestForceClose:
    def test_should_force_close_on_daily_limit(self):
        engine = _make_engine()
        assert engine.should_force_close(-31.0, -31.0) is True

    def test_should_force_close_on_overall_buffer(self):
        engine = _make_engine()
        assert engine.should_force_close(-50.0, -91.0) is True

    def test_no_force_close_within_limits(self):
        engine = _make_engine()
        assert engine.should_force_close(-10.0, -10.0) is False

    def test_force_close_plan(self):
        engine = _make_engine()
        now_et = datetime(2026, 8, 25, 10, 0)
        position = {"symbol": "TSLA", "qty": 10, "remaining_shares": 10}
        plan = engine.force_close_plan(position, now_et)
        assert plan["action"] == "close_position"
        assert plan["forced"] is True
        assert plan["delay"] > 0


class TestRecordAction:
    def test_record_action_increments_counter(self):
        engine = _make_engine()
        assert engine.daily_trades == 0
        engine.record_action()
        assert engine.daily_trades == 1

    def test_new_day_resets(self):
        engine = _make_engine()
        engine.record_action()
        engine.record_action()
        engine.new_day()
        assert engine.daily_trades == 0
