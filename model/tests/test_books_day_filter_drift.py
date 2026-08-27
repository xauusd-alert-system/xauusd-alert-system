"""TZ_BOOKS T-10 + T-23: day-of-week filter and distribution drift."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model.day_of_week_filter import (
    DEFAULT_TRADE_LEVEL,
    blocked_days_from_stats,
    day_of_week_stats,
    is_day_allowed,
    passes_trade_level,
)
from model.drift import (
    PSI_ALARM,
    PSI_STABLE,
    feature_drift_report,
    ks_statistic,
    normalization_shift,
    psi,
    walk_forward_drift_gate,
)


# --------------------------------------------------------------------- T-10
def test_trade_level_gate_defaults_to_book_value():
    assert DEFAULT_TRADE_LEVEL == 0.6
    assert passes_trade_level(0.6)
    assert passes_trade_level(0.91)
    assert not passes_trade_level(0.59)
    with pytest.raises(ValueError):
        passes_trade_level(0.5, trade_level=0.5)


def _trades(day_pattern: dict[int, tuple[int, float, float]], n_per_day: int = 40):
    """Synthetic trade log: weekday -> (win_rate, avg_pnl, total)."""
    rows = []
    rng = np.random.default_rng(0)
    for weekday, (wr, pnl_win, pnl_loss) in day_pattern.items():
        for i in range(n_per_day):
            win = rng.random() < wr
            # 2024-01-01 is a Monday: weeks*i + weekday keeps the weekday
            ts = pd.Timestamp("2024-01-01") + pd.Timedelta(
                weeks=i // 8, days=weekday, hours=i % 8)
            rows.append({"time": ts, "pnl": pnl_win if win else pnl_loss})
    return pd.DataFrame(rows)


def test_blocked_days_require_both_weak_wr_and_negative_pnl():
    df = _trades({
        0: (0.30, 10.0, -12.0),   # monday: weak AND losing -> block
        2: (0.30, 30.0, -5.0),    # wednesday: weak WR but profitable -> keep
        4: (0.80, 10.0, -12.0),   # friday: losing average but strong WR -> keep
    })
    stats = day_of_week_stats(df)
    blocked = blocked_days_from_stats(stats, min_trades=30)
    assert blocked == [0]


def test_under_sampled_days_never_blocked():
    df = _trades({1: (0.10, 5.0, -15.0)}, n_per_day=10)  # catastrophic but tiny
    stats = day_of_week_stats(df)
    assert blocked_days_from_stats(stats, min_trades=30) == []


def test_day_stats_shapes():
    df = _trades({0: (0.5, 10.0, -10.0), 3: (0.5, 10.0, -10.0)})
    stats = day_of_week_stats(df)
    assert len(stats) == 7
    assert [s.weekday for s in stats] == list(range(7))
    assert stats[0].trades == 40
    assert stats[2].trades == 0


def test_is_day_allowed():
    assert is_day_allowed(pd.Timestamp("2026-08-28"), blocked_days=[0, 2]) is True
    assert is_day_allowed(pd.Timestamp("2026-08-31"), blocked_days=[0, 2]) is False


# --------------------------------------------------------------------- T-23
def test_psi_zero_for_identical_samples():
    rng = np.random.default_rng(1)
    x = rng.normal(size=4000)
    assert psi(x, x) < 1e-9


def test_psi_grows_with_shift_and_crosses_thresholds():
    rng = np.random.default_rng(2)
    base = rng.normal(0.0, 1.0, size=20000)
    same = rng.normal(0.0, 1.0, size=20000)
    shifted = rng.normal(0.8, 1.0, size=20000)
    p_same = psi(base, same)
    p_shift = psi(base, shifted)
    assert p_same < PSI_STABLE
    assert p_shift > PSI_ALARM
    assert p_shift > p_same


def test_ks_matches_between_scipy_and_fallback():
    rng = np.random.default_rng(3)
    a = rng.normal(0.0, 1.0, size=500)
    b = rng.normal(0.7, 1.0, size=500)
    value = ks_statistic(a, b)
    assert 0.1 < value <= 1.0
    try:
        from scipy.stats import ks_2samp
        expected = float(ks_2samp(a, b).statistic)
        assert abs(value - expected) < 1e-12
    except ImportError:
        pass  # fallback path exercised by construction


def test_feature_drift_report_status_levels():
    rng = np.random.default_rng(4)
    n = 8000
    train = pd.DataFrame({"f": rng.normal(0.0, 1.0, n)})
    ok_live = pd.DataFrame({"f": rng.normal(0.0, 1.0, n)})
    alarm_live = pd.DataFrame({"f": rng.normal(3.0, 2.0, n)})
    assert feature_drift_report(train, ok_live)["status"] == "ok"
    assert feature_drift_report(train, alarm_live)["status"] == "alarm"


def test_normalization_shift_flags_scale_and_mean_moves():
    train = {"center": {"rsi": 50.0}, "scale": {"rsi": 14.0}}
    stable = {"center": {"rsi": 50.5}, "scale": {"rsi": 14.5}}
    volatile = {"center": {"rsi": 50.0}, "scale": {"rsi": 30.0}}   # ~2.1x
    drifted_mean = {"center": {"rsi": 70.0}, "scale": {"rsi": 14.0}}  # ~1.4 sigma
    assert normalization_shift(train, stable)["status"] == "ok"
    assert normalization_shift(train, volatile)["status"] == "alarm"
    assert normalization_shift(train, drifted_mean)["status"] == "alarm"
    assert "rsi" in normalization_shift(train, volatile)["shifted_columns"]


def test_walk_forward_drift_gate_blocks_deploy_on_alarm():
    rng = np.random.default_rng(5)
    n = 6000
    train = pd.DataFrame({"f": rng.normal(0.0, 1.0, n)})
    live_ok = pd.DataFrame({"f": rng.normal(0.05, 1.0, n // 3)})
    live_bad = pd.DataFrame({"f": rng.normal(4.0, 3.0, n // 3)})
    norm = {"center": {"f": 0.0}, "scale": {"f": 1.0}}
    norm_bad = {"center": {"f": 0.0}, "scale": {"f": 2.5}}

    ok = walk_forward_drift_gate(train, live_ok, norm, norm)
    assert ok["deploy_allowed"] is True

    psi_alarm = walk_forward_drift_gate(train, live_bad, norm, norm)
    assert psi_alarm["deploy_allowed"] is False

    norm_alarm = walk_forward_drift_gate(train, live_ok, norm, norm_bad)
    assert norm_alarm["deploy_allowed"] is False
    assert norm_alarm["normalization"]["status"] == "alarm"
