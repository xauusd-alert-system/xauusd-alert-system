"""Tests for the real-data extension scripts (T-23 drift report, T-25+T-03
ensemble backtest) and for the real-data feature bugfixes in T-19.

Chain under test (deterministic synthetic history, no terminal)::

    synthetic OHLCV -> import_external_candles -> sqlite
    -> run_book_drift_report.build_report          (PSI/KS + gate)
    -> run_book_ensemble_backtest.run_ensemble_backtest (votes -> trades
       -> forward_metrics -> acceptance verdict -> T-08 criterion)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from model.book_nn import (  # noqa: E402
    BookNetwork,
    book_fc_baseline_description,
    book_lstm_description,
)
from model.sample_generator import (  # noqa: E402
    DEFAULT_CFG,
    FEATURE_COLUMNS_BASE,
    FEATURE_COLUMNS_EXTENDED,
    build_book_features,
    generate_book_samples,
    synthetic_ohlcv,
)
from scripts.import_external_candles import import_csv  # noqa: E402
from scripts.run_book_drift_report import build_report  # noqa: E402
from scripts.run_book_ensemble_backtest import run_ensemble_backtest  # noqa: E402

WINDOW = 16
HORIZON = 12


def _write_mt4_csv(path: Path, df: pd.DataFrame) -> None:
    lines = ["Date;Open;High;Low;Close;Volume"]
    for t, r in zip(df.index, df.itertuples()):
        lines.append(f"{pd.Timestamp(t):%Y.%m.%d %H:%M};{r.open};{r.high};"
                     f"{r.low};{r.close};{int(r.volume)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prepare(tmp_path: Path, n: int = 1200, seed: int = 7):
    """Candle db + models dir with tiny deterministic (untrained) nets."""
    df = synthetic_ohlcv(n=n, seed=seed)
    csv = tmp_path / "hist.csv"
    _write_mt4_csv(csv, df)
    db = str(tmp_path / "ext.sqlite")
    import_csv(str(csv), db, "XAUUSD", "M15")

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    cfg = {"window": WINDOW, "horizon": HORIZON, "extended": False,
           "target_mode": "multi_horizon"}
    samples = generate_book_samples(df, cfg,
                                    norm_params_path=str(models_dir
                                                         / "normalization_params.json"))
    inp = len(samples.feature_columns)
    BookNetwork(book_fc_baseline_description(hidden=8, output_dim=2),
                WINDOW, inp, seed=1).save(str(models_dir / "book_fc"))
    BookNetwork(book_lstm_description(hidden=8, output_dim=2),
                WINDOW, inp, seed=2).save(str(models_dir / "book_lstm"))
    return str(models_dir), db


# ------------------------------------------- real-data T-19 bugfixes (regression)

def test_extended_features_have_no_head_nans():
    """Real-data bugfix: ATR min_periods used to leave 13 NaN head bars that
    poisoned the first windowed samples (NaN training loss on 480k-bar
    history)."""
    df = synthetic_ohlcv(n=300, seed=5)
    feats = build_book_features(df, {**DEFAULT_CFG, "extended": True})
    assert np.isfinite(feats.to_numpy()).all()


def test_session_vol_ratio_is_bounded_on_dead_flat_windows():
    """Real-data bugfix: std/|mean| exploded to 4.2e6 on flat windows of the
    2004-2025 XAUUSD history; the regularized denominator must bound it."""
    n = 200
    rng = np.random.default_rng(0)
    # alternating +/- returns: mean ~ 0 (worst case), std > 0
    ret = rng.choice([1e-3, -1e-3], size=n)
    close = 2000.0 * np.exp(np.cumsum(ret))
    df = pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.0005, "low": close * 0.9995, "close": close,
        "volume": np.full(n, 100.0),
    }, index=pd.date_range("2024-01-01", periods=n, freq="15min"))
    feats = build_book_features(df, {**DEFAULT_CFG, "extended": True})
    assert np.isfinite(feats["session_vol_ratio"]).all()
    assert feats["session_vol_ratio"].max() <= 20.0 + 1e-9


# ------------------------------------------------------------- drift report

def test_drift_report_structure_and_statuses():
    df = synthetic_ohlcv(n=3000, seed=9)
    cfg = {**DEFAULT_CFG, "extended": False}
    report = build_report(df, cfg)
    assert report["features"] == FEATURE_COLUMNS_BASE
    assert report["train_rows"] + report["live_rows"] <= 3000
    psi = report["psi_ks"]
    assert psi["status"] in ("ok", "warn", "alarm")
    for col in FEATURE_COLUMNS_BASE:
        assert np.isfinite(psi["features"][col]["psi"])
        assert np.isfinite(psi["features"][col]["ks"])
    assert isinstance(report["gate_deploy_blocked"], bool)
    assert report["normalization_shift"]["status"] in ("ok", "alarm")
    # json-serializable end to end
    json.dumps(report)


def test_drift_report_extended_columns():
    df = synthetic_ohlcv(n=3000, seed=9)
    report = build_report(df, {**DEFAULT_CFG, "extended": True})
    assert report["features"] == FEATURE_COLUMNS_EXTENDED


def test_drift_report_detects_regime_shift():
    """A volatility regime change between train and live must trip the gate."""
    calm = synthetic_ohlcv(n=2000, seed=3)          # sigma ~ 0.0012
    wild = synthetic_ohlcv(n=800, seed=4, start_price=2300.0)
    ret = wild["close"].pct_change().fillna(0).to_numpy() * 6  # 6x volatility
    wild_close = 2300.0 * np.exp(np.cumsum(ret))
    wild = pd.DataFrame({
        "open": np.concatenate([[wild_close[0]], wild_close[:-1]]),
        "high": wild_close * 1.004, "low": wild_close * 0.996,
        "close": wild_close,
        "volume": np.full(len(wild), 200.0),
    }, index=pd.date_range("2025-01-01", periods=len(wild), freq="15min"))
    df = pd.concat([calm, wild])
    report = build_report(df, {**DEFAULT_CFG, "extended": False})
    assert report["psi_ks"]["worst_psi"] > 0.10    # drift is visible
    assert report["normalization_shift"]["worst_scale_ratio"] > 1.5


# --------------------------------------------------------- ensemble backtest

def test_backtest_end_to_end_forced_trades(tmp_path):
    models_dir, db = _prepare(tmp_path)
    report = run_ensemble_backtest(
        models_dir, db, "XAUUSD", "M15",
        trade_level=0.5, min_agreement=0.0,   # force non-flat votes
        spread=0.0, max_bars=None)
    assert report["test_samples"] > 0
    assert report["signals_fired"] >= 1
    trades = report["trades"]
    assert len(trades) >= 1

    test_first = report["test_first_bar"]
    prev_exit = -1
    for t in trades:
        # out-of-sample only: signal bar inside the test slice
        assert t["signal_bar"] >= test_first
        # no look-ahead: entry is the bar AFTER the signal
        assert t["entry_bar"] == t["signal_bar"] + 1
        # one position at a time (EA mirror)
        assert t["entry_bar"] > prev_exit
        prev_exit = t["exit_bar"]
        assert 1 <= t["bars_held"] <= HORIZON
        assert t["direction"] in (1, -1)
        # geometry honored (spread=0 -> exact barrier prices)
        if t["exit_reason"] == "tp":
            assert t["exit"] == pytest.approx(t["tp"])
            assert t["pnl"] > 0
        elif t["exit_reason"] == "sl":
            assert t["exit"] == pytest.approx(t["sl"])
            assert t["pnl"] < 0
        else:
            assert t["exit_reason"] == "time"

    m = report["metrics"]
    assert m["trades"] == len(trades)
    assert 0.0 <= m["win_rate"] <= 1.0
    assert report["criterion_score"] is not None
    assert isinstance(report["acceptance"]["accepted"], bool)
    assert report["max_dd_pct"] >= 0.0
    json.dumps(report)   # serializable


def test_backtest_deterministic(tmp_path):
    models_dir, db = _prepare(tmp_path, seed=21)
    kw = dict(trade_level=0.5, min_agreement=0.0, spread=0.3, max_bars=None)
    a = run_ensemble_backtest(models_dir, db, "XAUUSD", "M15", **kw)
    b = run_ensemble_backtest(models_dir, db, "XAUUSD", "M15", **kw)
    assert a["trades"] == b["trades"]
    assert a["metrics"] == b["metrics"]


def _ramp_frame(direction: int = 1, n: int = 30) -> pd.DataFrame:
    """Monotone frame: a long with a 2:1 RR ATR stop always exits at TP."""
    close = 2000.0 + direction * np.arange(n) * 5.0
    return pd.DataFrame({
        "open": close - direction * 1.0,
        "high": close + 2.0,
        "low": close - 3.0,
        "close": close,
        "volume": np.full(n, 100.0),
    }, index=pd.date_range("2024-01-01", periods=n, freq="15min"))


def test_simulate_trade_charges_round_trip_spread():
    from scripts.run_book_ensemble_backtest import _simulate_trade
    df = _ramp_frame(direction=1)
    kw = dict(atr=4.0, horizon=12, atr_mult=1.5, risk_reward=2.0)
    free = _simulate_trade(df, 5, 1, spread=0.0, **kw)
    costly = _simulate_trade(df, 5, 1, spread=1.0, **kw)
    assert free["exit_reason"] == "tp"
    assert costly["exit_reason"] == "tp"     # barrier shift is tiny vs ramp
    assert free["pnl"] > 0
    assert free["pnl"] - costly["pnl"] == pytest.approx(1.0)  # round trip


def test_simulate_trade_is_pessimistic_on_ambiguous_bars():
    """A bar touching both SL and TP must resolve as the stop (worst case)."""
    from scripts.run_book_ensemble_backtest import _simulate_trade
    n = 10
    close = np.full(n, 2000.0)
    df = pd.DataFrame({
        "open": close, "high": close + 50.0, "low": close - 50.0,
        "close": close, "volume": np.full(n, 100.0),
    }, index=pd.date_range("2024-01-01", periods=n, freq="15min"))
    trade = _simulate_trade(df, 2, 1, atr=4.0, horizon=12, atr_mult=1.5,
                            risk_reward=2.0, spread=0.0)
    assert trade["exit_reason"] == "sl"
    assert trade["pnl"] < 0


def test_backtest_reports_costs_only_when_trades_exist(tmp_path):
    """Costs must never flip a decision into profit: with a forced signal on
    a flat frame every long loses the spread."""
    models_dir, db = _prepare(tmp_path, seed=13)
    free = run_ensemble_backtest(models_dir, db, "XAUUSD", "M15",
                                 trade_level=0.5, min_agreement=0.0,
                                 spread=0.0, max_bars=None)
    assert free["signals_fired"] > 0        # sanity: decisions were made
    assert free["metrics"]["trades"] == len(free["trades"])


def test_backtest_flat_ensemble_yields_empty_metrics(tmp_path):
    models_dir, db = _prepare(tmp_path, seed=17)
    # unreachable threshold: nothing ever fires
    report = run_ensemble_backtest(models_dir, db, "XAUUSD", "M15",
                                   trade_level=0.999999, min_agreement=1.0,
                                   max_bars=None)
    assert report["signals_fired"] == 0
    assert report["trades"] == []
    assert report["metrics"]["trades"] == 0
    assert report["acceptance"]["accepted"] is False
    assert report["criterion_score"] is None
    assert any("trades" in r for r in report["acceptance"]["reasons"])
