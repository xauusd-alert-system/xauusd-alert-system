"""
Unit tests for regime/classifier.py.
Run with: pytest regime/tests/test_classifier.py -v
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles
from features.indicators import build_all_indicators
from regime.classifier import add_regime_indicators, classify_regime_series, classify_regime_row, RegimeLabel
from regime.ml_interface import RuleBasedRegimeClassifier, MLRegimeClassifierStub

CFG = load_config()
SESSIONS = CFG["sessions"]


def _prepared_df(n=300):
    df = fetch_mock_candles("M15", n_candles=n, sessions_config=SESSIONS)
    df = build_all_indicators(df, CFG)
    df = add_regime_indicators(df, CFG)
    return df


def test_warmup_period_forces_no_trade():
    df = _prepared_df(n=300)
    labels = classify_regime_series(df, CFG)
    min_candles = CFG["regime"]["min_candles_for_regime"]
    assert (labels.iloc[:min_candles] == RegimeLabel.NO_TRADE).all()


def test_classify_returns_valid_enum_values():
    df = _prepared_df(n=300)
    labels = classify_regime_series(df, CFG)
    valid_labels = set(RegimeLabel)
    assert set(labels.unique()) <= valid_labels


def test_synthetic_strong_uptrend_classified_as_trend_up():
    """Construct a deterministic strong uptrend and verify classifier picks it up."""
    n = 250
    ts = np.arange(n) * 900
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 1.5, n)
    close = 2000 + np.arange(n) * 3.0 + noise
    df = pd.DataFrame({
        "timestamp_utc": ts,
        "open": close - 0.5,
        "high": close + np.abs(rng.normal(1.0, 0.3, n)),
        "low":  close - np.abs(rng.normal(1.0, 0.3, n)),
        "close": close,
        "volume": np.full(n, 100.0),
        "session": ["london"] * n,
    })
    df = build_all_indicators(df, CFG)
    df = add_regime_indicators(df, CFG)
    labels = classify_regime_series(df, CFG)
    tail_labels = labels.iloc[-30:]
    assert (tail_labels == RegimeLabel.TREND_UP).sum() >= 10
    tail_labels = labels.iloc[-30:]
    assert (tail_labels == RegimeLabel.TREND_UP).sum() >= 10


def test_no_trade_on_flat_zero_volatility_series():
    """A perfectly flat price series has zero ATR -> must be classified no_trade, never a trend."""
    n = 250
    ts = np.arange(n) * 900
    flat_price = np.full(n, 2000.0)
    df = pd.DataFrame({
        "timestamp_utc": ts,
        "open": flat_price,
        "high": flat_price,
        "low": flat_price,
        "close": flat_price,
        "volume": np.full(n, 100.0),
        "session": ["asia"] * n,
    })
    df = build_all_indicators(df, CFG)
    df = add_regime_indicators(df, CFG)
    labels = classify_regime_series(df, CFG)
    assert (labels.iloc[-20:] == RegimeLabel.NO_TRADE).all()


def test_rule_based_adapter_matches_direct_call():
    df = _prepared_df(n=300)
    adapter = RuleBasedRegimeClassifier(CFG)
    direct_labels = classify_regime_series(df, CFG)
    adapter_labels = adapter.classify_series(df)
    assert (direct_labels == adapter_labels).all()


def test_ml_stub_raises_not_implemented():
    """ML regime classifier must fail loudly if used before training - never silently fallback."""
    stub = MLRegimeClassifierStub()
    df = _prepared_df(n=50)
    try:
        stub.classify(df.iloc[-1])
        assert False, "Expected NotImplementedError"
    except NotImplementedError:
        pass

