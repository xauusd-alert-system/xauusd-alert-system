"""Tests for risk/sizing.py — sizing functions moved from execution/risk_sizer.py.

Covers:
  - P1-4: cluster_exposure_ok REQUIRES cluster_cap/total_cap (no defaults);
  - lots_for_risk "never round up" skip rule;
  - P1-7: drawdown_throttle levels against the persistent HWM.
"""

import inspect

import numpy as np
import pytest

from risk.sizing import (
    cluster_exposure_ok,
    drawdown_throttle,
    leverage_multiplier,
    lots_for_risk,
    trade_risk_pct,
    vol_target_scale,
)


def test_cluster_exposure_ok_with_caps():
    cur = {"fx": 0.002, "metals": 0.001}
    ok = cluster_exposure_ok(cur, "fx", 0.003, cluster_cap=0.004, total_cap=0.0075)
    assert not ok["ok"]  # 0.002+0.003 = 0.005 > 0.004
    ok2 = cluster_exposure_ok(cur, "metals", 0.002, cluster_cap=0.004, total_cap=0.0075)
    assert ok2["ok"]  # 0.001+0.002 = 0.003 <= 0.004; total 0.005 <= 0.0075


def test_cluster_exposure_ok_caps_are_required_p1_4():
    """P1-4: the caps have NO defaults — omitting them is a TypeError."""
    sig = inspect.signature(cluster_exposure_ok)
    for name in ("cluster_cap", "total_cap"):
        assert sig.parameters[name].default is inspect.Parameter.empty, name
    with pytest.raises(TypeError):
        cluster_exposure_ok({"fx": 0.001}, "fx", 0.001)


def test_lots_for_risk_never_rounds_up():
    r = lots_for_risk(10000, 0.0025, sl_ticks=300, tick_value_per_lot=0.1, min_lot=0.01)
    assert not r["skipped"]
    assert r["lots"] == pytest.approx(0.8333, abs=1e-3)

    r2 = lots_for_risk(100, 0.0025, sl_ticks=300, tick_value_per_lot=0.1, min_lot=0.01)
    assert r2["skipped"]


def test_lots_for_risk_non_positive_inputs():
    r = lots_for_risk(0, 0.0025, sl_ticks=300, tick_value_per_lot=0.1)
    assert r["skipped"] and r["reason"] == "non-positive inputs"


def test_drawdown_throttle_levels():
    assert drawdown_throttle(-0.03) == 1.0
    assert drawdown_throttle(-0.05) == 0.75
    assert drawdown_throttle(-0.07) == 0.5
    assert drawdown_throttle(-0.09) == 0.0


def test_drawdown_throttle_uses_persistent_hwm_semantics():
    """P1-7: the function itself is pure — the HWM persistence/ratchet is
    RiskState's job; the throttle just consumes the current DD from it."""
    hwm = 10_000.0
    for equity, expected in ((9_900.0, 1.0), (9_500.0, 0.75), (9_300.0, 0.5), (9_100.0, 0.0)):
        assert drawdown_throttle(equity / hwm - 1.0) == expected


def test_trade_risk_pct_scale():
    r1 = trade_risk_pct(0.10, 0.4, trades_per_day=2, enb=1.0)
    r2 = trade_risk_pct(0.10, 0.4, trades_per_day=2, enb=3.0)
    assert r1 > r2  # more effective bets -> smaller per-trade risk


def test_leverage_and_vol_scale():
    assert leverage_multiplier(0.10, 0.20) == pytest.approx(0.5)
    assert leverage_multiplier(0.10, 0.05) == pytest.approx(1.25)
    r = np.full(40, 0.002)
    scale = vol_target_scale(r, 0.10, periods_per_year=250, ewma_span=20)
    assert scale[0] == 1.0  # warm-up
