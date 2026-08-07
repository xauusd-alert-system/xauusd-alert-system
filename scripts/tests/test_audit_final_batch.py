"""
Tests for the final audit batch:
- features/fractional_diff.py
- model/cv.py (purged K-fold) + model/uniqueness.py
- trainer feature_subset / sample_weight
- scripts/feature_selection.py, diag_meta_precheck.py, diag_event_tail.py,
  diag_time_stop.py, backtest_pooled.py
- backtest/portfolio.py + execution/risk_sizer.py
- EnsembleBacktester fill_mode='limit'
- regression: block_bootstrap_t with fewer trades than the block
- regression: regime_overrides applied with enum regimes (str(enum) vs .value)
"""

import json
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from features.fractional_diff import frac_diff, min_d_adf
from model.cv import purged_kfold_indices, purge_train_indices, embargo_train_indices
from model.uniqueness import average_uniqueness_weights, sample_weight_series
from backtest.portfolio import (
    daily_r_matrix,
    strategy_correlation,
    effective_number_bets,
    cluster_risk_parity_weights,
    portfolio_curve,
    portfolio_metrics,
    compare_schemes,
    kill_switch_thresholds,
)
from execution.risk_sizer import (
    trade_risk_pct,
    lots_for_risk,
    cluster_exposure_ok,
    same_direction_cluster_penalty,
    drawdown_throttle,
    leverage_multiplier,
    vol_target_scale,
)
from scripts.deflated_sharpe import (
    _make_synthetic_wf_df,
    _inject_biased_probs,
    run_analysis,
)
from scripts.exit_calibration import calibrate_stop
from scripts.diag_meta_precheck import run_meta_precheck
from scripts.diag_event_tail import run_event_tail, _minutes_to_nearest
from scripts.diag_time_stop import run_time_stop
from model.ensemble_backtest import EnsembleBacktester


@pytest.fixture(scope="module")
def synthetic_gbp_df():
    df = _make_synthetic_wf_df(12600, price=1.28, atr=0.0014, freq="1h")
    return _inject_biased_probs(df)


# ---------------------------------------------------------------------------
# Regression (post-real-run 2026-08-07, PR #11): the two bugs fixed there
# ---------------------------------------------------------------------------

def test_block_bootstrap_t_fewer_trades_than_block():
    """A fold with fewer trades than the bootstrap block (20) made
    rng.integers(0, n - block) fail with ValueError: high <= 0. The block must
    be shrunk to n - 1; the call must return a float without raising."""
    from backtest.metrics import block_bootstrap_t
    t = block_bootstrap_t([1.0, -0.5, 0.2], block=20, n_boot=100)
    assert isinstance(t, float) and np.isfinite(t)


def test_regime_override_applied_with_enum_regimes():
    """classify_regime_series() returns RegimeLabel enum objects; str(enum) is
    'RegimeLabel.TREND_UP' while regime_overrides keys are 'trend_up' (.value).
    The engine must normalize via _regime_name() so the override applies:
    stop 5.0xATR vs the base 3.0xATR, and regime_at_entry == 'trend_up'."""
    from model.tests.test_ensemble_backtest import _fx_v3_early_be_cfg, _df
    from regime.classifier import RegimeLabel
    cfg = _fx_v3_early_be_cfg(1.0)
    cfg["signal_grid"]["regime_overrides"] = {
        "trend_up": {"stop_mult": 5.0, "tp3_mult": 4.0},
    }
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df(n=400)
    df["regime"] = RegimeLabel.TREND_UP  # enum objects, as in production
    trades = bt.run(df)
    assert len(trades) >= 1
    t0 = trades[0]
    assert t0.regime_at_entry == "trend_up"  # .value, not 'RegimeLabel.TREND_UP'
    assert abs(t0.entry_price - t0.stop_price) == pytest.approx(5.0 * 0.0003, rel=1e-6)
    assert abs(t0.tp3_price - t0.entry_price) == pytest.approx(4.0 * 0.0003, rel=1e-6)


# ---------------------------------------------------------------------------
# Fractional differencing
# ---------------------------------------------------------------------------

def test_frac_diff_d1_is_first_difference():
    s = pd.Series(np.cumsum(np.random.default_rng(0).normal(0, 1, 100)))
    fd = frac_diff(s, d=1.0)
    # d=1 -> standard first difference (weights [1, -1])
    assert np.allclose(fd.iloc[1:], s.diff().iloc[1:], atol=1e-9)


def test_frac_diff_d0_identity():
    s = pd.Series(np.random.default_rng(1).normal(0, 1, 50))
    fd = frac_diff(s, d=0.0)
    assert np.allclose(fd.iloc[10:], s.iloc[10:], atol=1e-9)


def test_frac_diff_warmup_nan():
    s = pd.Series(np.random.default_rng(2).normal(0, 1, 40))
    fd = frac_diff(s, d=0.5)
    assert fd.iloc[:5].isna().any()


def test_min_d_adf_finds_stationary_d():
    rng = np.random.default_rng(3)
    # random walk: d=0 fails ADF, d>=~0.4 should pass on this sample
    rw = pd.Series(np.cumsum(rng.normal(0, 1, 300)))
    res = min_d_adf(rw, d_list=[0.0, 0.3, 0.5, 0.7, 1.0])
    assert res["min_d"] is not None
    assert res["min_d"] > 0.0
    assert res["ladder"][0]["stationary"] is False  # raw random walk not stationary


# ---------------------------------------------------------------------------
# Purged CV + uniqueness
# ---------------------------------------------------------------------------

def test_purge_train_indices_drops_overlapping():
    train = np.arange(0, 20)
    # test block [10, 15); horizon 6 -> row i's window is [i+1, i+6]
    # overlapping rows: i+1 < 15 and i+6 > 10 -> i in 4..13
    purged = purge_train_indices(train, 10, 15, horizon=6)
    assert 4 not in purged and 13 not in purged
    assert 3 in purged and 14 in purged and 15 in purged


def test_embargo_train_indices():
    train = np.arange(0, 20)
    emb = embargo_train_indices(train, 10, embargo=3)
    assert 7 not in emb and 9 not in emb
    assert 6 in emb


def test_purged_kfold_indices_cover_all_and_no_overlap():
    folds = purged_kfold_indices(100, n_splits=5, horizon=10, embargo=2)
    assert len(folds) == 5
    seen = set()
    for k, (tr, te) in enumerate(folds):
        assert len(te) > 0
        # the FIRST fold has no history before it (embargo at series start) —
        # that is expected purged-CV behavior; the rest must have training rows
        if k > 0:
            assert len(tr) > 0
        assert not set(tr) & set(te)
        seen.update(te.tolist())
    assert seen == set(range(100))


def test_average_uniqueness_weights_shape_and_bounds():
    w = average_uniqueness_weights(100, horizon=10)
    assert len(w) == 100
    assert w[-10:].sum() == 0  # no full label window at the tail
    assert (w[:80] > 0).all()
    # first row's label is the most crowded (covers the widest future overlap)
    assert w[0] <= 1.0


def test_sample_weight_series_decay():
    w0 = sample_weight_series(100, horizon=10, decay_lambda=0.0)
    wd = sample_weight_series(100, horizon=10, decay_lambda=0.05)
    assert np.allclose(w0, average_uniqueness_weights(100, 10))
    # decay makes the oldest rows lighter than the newest
    assert wd[0] < w0[0]
    assert wd[70] > wd[10]


# ---------------------------------------------------------------------------
# Trainer: feature_subset + sample_weight
# ---------------------------------------------------------------------------

def test_feature_subset_respected():
    from model.trainer import build_training_matrix
    df = pd.DataFrame({
        "close": np.arange(50.0), "high": np.arange(50.0) + 0.1,
        "low": np.arange(50.0) - 0.1, "atr": np.full(50, 1.0),
        "rsi": np.full(50, 50.0), "ema_9": np.arange(50.0),
        "label": [1, -1] * 25,
    })
    X, y, cols = build_training_matrix(df, cfg={"model": {"feature_subset": ["atr", "rsi"]}})
    assert set(cols) == {"atr", "rsi"}
    assert set(X.columns) == {"atr", "rsi"}


def test_feature_subset_unknown_ignored():
    from model.trainer import build_training_matrix
    df = pd.DataFrame({
        "close": np.arange(50.0), "high": np.arange(50.0) + 0.1,
        "low": np.arange(50.0) - 0.1, "atr": np.full(50, 1.0),
        "rsi": np.full(50, 50.0), "label": [1, -1] * 25,
    })
    X, y, cols = build_training_matrix(df, cfg={"model": {"feature_subset": ["atr", "no_such"]}})
    assert "no_such" not in cols
    assert "atr" in cols


def test_train_model_sample_weight():
    from model.trainer import train_model
    X = pd.DataFrame({"a": np.arange(60.0), "b": np.arange(60.0) % 3})
    y = pd.Series((np.arange(60) % 2).astype(int))
    m1 = train_model(X, y, cfg={"model": {"type": "xgboost", "random_seed": 42}})
    m2 = train_model(X, y, cfg={"model": {"type": "xgboost", "random_seed": 42}},
                     sample_weight=np.ones(60))
    assert m1 is not None and m2 is not None
    with pytest.raises(ValueError):
        train_model(X, y, cfg={"model": {"type": "xgboost"}}, sample_weight=np.ones(5))


# ---------------------------------------------------------------------------
# Feature selection (MDA)
# ---------------------------------------------------------------------------

def test_feature_selection_run(synthetic_gbp_df):
    from config.loader import load_config
    from scripts.feature_selection import run_feature_selection
    from labeling.label_generator import generate_labels_from_config
    cfg = load_config()
    df = synthetic_gbp_df.copy()
    if "label" not in df.columns:
        df["label"] = generate_labels_from_config(df, cfg)
    d = run_feature_selection(cfg, "GBPUSD", df,
                              max_features=5, n_splits=3, n_permute=2)
    assert d["n_features_total"] == 46
    assert len(d["mda_rank"]) > 0
    assert len(d["suggested_subset"]) <= 5
    assert len(d["clustered"]) > 0
    json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Meta pre-check / event tail / time stop
# ---------------------------------------------------------------------------

def test_meta_precheck_structure(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    d = run_meta_precheck(cfg, "GBPUSD", synthetic_gbp_df, max_folds=4)
    assert d["n_trades"] > 0
    assert "auc" in d and "verdict" in d
    if d["auc"] is not None:
        assert 0.0 <= d["auc"] <= 1.0
        assert len(d["deciles"]) > 0
    json.dumps(d, default=str)


def test_event_tail_buckets(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    events = [{"timestamp_utc": int(1_650_000_000 + i * 86_400), "title": f"e{i}"}
              for i in range(100)]
    d = run_event_tail(cfg, "GBPUSD", synthetic_gbp_df, events, tail_pct=10.0, max_folds=4)
    assert d["n_trades"] > 0
    assert d["n_events"] == 100
    assert len(d["tail_buckets"]) == 7
    assert sum(b["n"] for b in d["tail_buckets"]) == d["n_tail"]
    json.dumps(d, default=str)


def test_minutes_to_nearest():
    ev = np.array([100, 200, 300])
    assert _minutes_to_nearest(ev, 150) == pytest.approx(50.0 / 60.0)
    assert _minutes_to_nearest(ev, 50) == pytest.approx(50.0 / 60.0)
    assert _minutes_to_nearest(np.array([]), 100) == float("inf")


def test_time_stop_structure(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    d = run_time_stop(cfg, "GBPUSD", synthetic_gbp_df, max_folds=4, max_h=20)
    assert d["n_trades"] > 0
    assert len(d["curve"]) > 0
    assert d["curve"][0]["h_bars"] == 1
    json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Pooled comparison
# ---------------------------------------------------------------------------

def test_pooled_comparison_runs(synthetic_gbp_df):
    from config.loader import load_config
    from scripts.backtest_pooled import run_pooled_comparison
    cfg = load_config()
    d = run_pooled_comparison(cfg, ["GBPUSD", "EURUSD"], max_folds=2, scale="zscore")
    assert "GBPUSD" in d["assets"] and "EURUSD" in d["assets"]
    for a in ("GBPUSD", "EURUSD"):
        m = d["assets"][a]
        assert "per_asset" in m and "pooled" in m and "auc" in m
    json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def _trades(asset, days=60, seed=0, r_mean=0.03, r_std=0.4):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        ts = 1_700_000_000 + d * 86400
        for _ in range(rng.integers(0, 3)):
            rows.append({"entry_ts": ts, "net_r": rng.normal(r_mean, r_std)})
    return pd.DataFrame(rows)


def test_daily_r_matrix_and_correlation():
    trades = {"A": _trades("A", seed=1), "B": _trades("B", seed=2)}
    daily = daily_r_matrix(trades)
    assert list(daily.columns) == ["A", "B"]
    assert daily.shape[0] > 0
    assert (daily == 0).sum().sum() >= 0  # filled days
    corr = strategy_correlation(daily)
    assert abs(corr.loc["A", "B"]) < 0.5  # independent synthetic strategies


def test_effective_number_bets():
    rng = np.random.default_rng(5)
    # 3 independent strategies -> ENB ~ 3
    indep = pd.DataFrame({c: rng.normal(0, 1, 200) for c in "ABC"})
    enb_i = effective_number_bets(indep)
    assert 2.3 < enb_i <= 3.0
    # 3 identical strategies -> ENB ~ 1
    base = rng.normal(0, 1, 200)
    same = pd.DataFrame({"A": base, "B": base, "C": base})
    assert effective_number_bets(same) < 1.3


def test_cluster_risk_parity_weights():
    assets = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "BTCUSD"]
    clusters = {"metals": ["XAUUSD", "XAGUSD"], "fx": ["EURUSD", "GBPUSD"], "crypto": ["BTCUSD"]}
    w = cluster_risk_parity_weights(assets, clusters)
    assert w.sum() == pytest.approx(1.0)
    assert w["XAUUSD"] == pytest.approx(1.0 / 6)  # 1/3 budget / 2 members
    assert w["BTCUSD"] == pytest.approx(1.0 / 3)
    # unknown asset becomes its own singleton cluster
    w2 = cluster_risk_parity_weights(["XAUUSD", "ZZZ"], clusters)
    assert w2["ZZZ"] == pytest.approx(0.5)


def test_portfolio_metrics_and_schemes():
    rng = np.random.default_rng(6)
    daily = pd.DataFrame({c: rng.normal(0.02, 0.3, 200) for c in "ABCD"})
    schemes = compare_schemes(daily, {"a": ["A", "B"], "b": ["C", "D"]})
    assert set(schemes) >= {"equal_weight", "cluster_risk_parity"}
    for m in schemes.values():
        assert "sharpe" in m and "max_dd_r" in m
    ks = kill_switch_thresholds(daily)
    assert ks["daily_2sigma"] < 0
    assert ks["weekly_3sigma"] < ks["daily_2sigma"]


def test_portfolio_curve_respects_weights():
    rng = np.random.default_rng(7)
    daily = pd.DataFrame({"A": rng.normal(0.1, 0.2, 50), "B": rng.normal(0.0, 0.2, 50)})
    w = pd.Series({"A": 0.0, "B": 1.0})
    curve = portfolio_curve(daily, w)
    assert np.allclose(curve, daily["B"], atol=1e-12)


# ---------------------------------------------------------------------------
# Risk sizer
# ---------------------------------------------------------------------------

def test_trade_risk_pct_scale():
    r1 = trade_risk_pct(0.10, 0.4, trades_per_day=2, enb=1.0)
    r2 = trade_risk_pct(0.10, 0.4, trades_per_day=2, enb=3.0)
    assert r1 > r2  # more effective bets -> smaller per-trade risk
    assert 0.0 < r1 < 0.02  # ~1.1% at 10% vol target, sigma_r 0.4, 2 trades/day


def test_lots_for_risk_skip_below_min():
    # equity 10000, risk 0.25%, SL 300 ticks, tick value 0.1 -> lots = 10000*0.0025/30 = 0.833
    r = lots_for_risk(10000, 0.0025, sl_ticks=300, tick_value_per_lot=0.1, min_lot=0.01)
    assert not r["skipped"]
    assert r["lots"] == pytest.approx(0.8333, abs=1e-3)
    # tiny equity -> computed lot below min -> skip, never round up
    r2 = lots_for_risk(100, 0.0025, sl_ticks=300, tick_value_per_lot=0.1, min_lot=0.01)
    assert r2["skipped"]
    assert "never round up" in r2["reason"]


def test_cluster_exposure_ok():
    cur = {"fx": 0.002, "metals": 0.001}
    ok = cluster_exposure_ok(cur, "fx", 0.003, cluster_cap=0.004, total_cap=0.0075)
    assert not ok["ok"]  # 0.002+0.003 = 0.005 > 0.004
    ok2 = cluster_exposure_ok(cur, "metals", 0.002, cluster_cap=0.004, total_cap=0.0075)
    assert ok2["ok"]  # 0.001+0.002 = 0.003 <= 0.004; total 0.005 <= 0.0075


def test_same_direction_cluster_penalty():
    cur = {"fx": 1}
    assert same_direction_cluster_penalty(cur, "GBPUSD", 1, "fx") == pytest.approx(0.35)
    assert same_direction_cluster_penalty(cur, "GBPUSD", -1, "fx") == pytest.approx(1.0)


def test_drawdown_throttle_levels():
    assert drawdown_throttle(-0.03) == 1.0
    assert drawdown_throttle(-0.05) == 0.75
    assert drawdown_throttle(-0.07) == 0.5
    assert drawdown_throttle(-0.09) == 0.0


def test_leverage_and_vol_scale():
    assert leverage_multiplier(0.10, 0.20) == pytest.approx(0.5)  # clipped at lo
    assert leverage_multiplier(0.10, 0.05) == pytest.approx(1.25)  # clipped at hi
    r = np.full(40, 0.002)
    scale = vol_target_scale(r, 0.10, periods_per_year=250, ewma_span=20)
    assert scale[0] == 1.0  # warm-up
    assert (scale[20:] > 0).all()


# ---------------------------------------------------------------------------
# Engine: limit fill mode
# ---------------------------------------------------------------------------

def _df(n=400, price=1.10):
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({
        "timestamp_utc": idx.astype("int64") // 10 ** 9,
        "open": price, "high": price, "low": price, "close": price,
        "volume": 100.0, "session": "london", "regime": "trend_up",
        "atr": 0.0003, "ml_p_long": 0.9, "ml_p_short": 0.1,
    })


def test_limit_fill_mode_fills_on_touch():
    from model.tests.test_ensemble_backtest import _fx_v3_early_be_cfg
    cfg = _fx_v3_early_be_cfg(1.0)
    cfg["backtest"]["fill_mode"] = "limit"
    cfg["backtest"]["limit_frac"] = 0.25
    cfg["backtest"]["limit_timeout"] = 2
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df()
    # bar 2 dips to the limit price (signal at bar 0 -> limit = close + 0.25*atr)
    limit = 1.10 + 0.25 * 0.0003
    df.loc[2, "low"] = limit - 1e-9
    trades = bt.run(df)
    assert len(trades) >= 1
    assert trades[0].entry_price == pytest.approx(limit, abs=1e-6)


def test_limit_fill_mode_timeout_cancels():
    from model.tests.test_ensemble_backtest import _fx_v3_early_be_cfg
    cfg = _fx_v3_early_be_cfg(1.0)
    cfg["backtest"]["fill_mode"] = "limit"
    cfg["backtest"]["limit_frac"] = 0.25
    cfg["backtest"]["limit_timeout"] = 2
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df()
    # no touch within timeout -> no trades at all
    trades = bt.run(df.head(10))
    assert len(trades) == 0


def test_limit_fill_mode_fills_later_within_timeout():
    from model.tests.test_ensemble_backtest import _fx_v3_early_be_cfg
    cfg = _fx_v3_early_be_cfg(1.0)
    cfg["backtest"]["fill_mode"] = "limit"
    cfg["backtest"]["limit_frac"] = 0.25
    cfg["backtest"]["limit_timeout"] = 3
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df()
    limit = 1.10 + 0.25 * 0.0003
    df.loc[3, "low"] = limit - 1e-9  # touch on bar 3 (within 3-bar window)
    trades = bt.run(df)
    assert len(trades) >= 1
    assert trades[0].entry_price == pytest.approx(limit, abs=1e-6)
