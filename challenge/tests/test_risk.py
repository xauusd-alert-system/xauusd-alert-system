"""Tests for the challenge risk rules (sizing + half-limit cushions)."""
import pytest

from challenge.risk import ChallengeRisk

CFG = {
    "risk": {
        "per_trade_risk_usd": 5, "daily_loss_stop": 25, "total_loss_stop": 60,
        "daily_profit_lock": 20, "max_open_positions": 2, "max_leverage": 5,
        "stop_pct": 0.005, "tp_ratio": 1.5,
    }
}


def _risk():
    return ChallengeRisk(CFG)


def test_position_size_respects_risk_and_leverage():
    r = _risk()
    # $150 stock, $5 risk at 0.5% stop ($0.75) -> ~6 shares; 1:5 of $1000 caps
    # notional at $5000 -> ~33 shares. Risk bound wins.
    assert r.position_size(150.0, 1000.0) == 6
    # Tiny price -> risk-bound is huge, leverage cap bounds it.
    assert r.position_size(2.0, 1000.0) <= 2500
    assert r.position_size(0.0, 1000.0) == 0
    assert r.position_size(150.0, 0.0) == 0


def test_stop_tp_geometry():
    r = _risk()
    ls, lt = r.stop_tp(150.0, "long")
    assert ls == pytest.approx(150.0 * (1 - 0.005))
    assert lt == pytest.approx(150.0 * (1 + 0.005 * 1.5))
    ss, st = r.stop_tp(150.0, "short")
    assert ss == pytest.approx(150.0 * (1 + 0.005))
    assert st == pytest.approx(150.0 * (1 - 0.005 * 1.5))


def test_evaluate_trade():
    r = _risk()
    assert r.evaluate(1010.0, 1000.0, 1000.0)[0] == "trade"


def test_evaluate_daily_loss_stop():
    r = _risk()
    action, reason = r.evaluate(970.0, 1000.0, 1000.0)
    assert action == "flatten_day"
    assert "daily loss" in reason


def test_evaluate_daily_profit_lock():
    r = _risk()
    assert r.evaluate(1022.0, 1000.0, 1000.0)[0] == "flatten_day"


def test_evaluate_total_loss_stop():
    r = _risk()
    # Total floor breaches even on a day that started fresh: must HALT, not
    # just flatten the day (otherwise we'd restart next day already below the
    # platform's -$100 total floor).
    action, reason = r.evaluate(935.0, 1000.0, 1000.0)
    assert action == "halt"
    assert "total loss" in reason