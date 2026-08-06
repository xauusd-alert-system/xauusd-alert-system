"""
Tests for Smart Money & Institutional Microstructure Metrics.
Verifies calculation of Manipulation Index, Zone Strength, SMF Ratio,
Liquidity Grab, Delta Confidence, and report formatting.
"""
import numpy as np
import pandas as pd
import pytest

from features.smart_money_metrics import (
    calculate_manipulation_index,
    calculate_zone_strength,
    calculate_smf_ratio,
    calculate_liquidity_grab,
    calculate_delta_confidence,
    compute_institutional_metrics,
    format_institutional_metrics_report,
)


@pytest.fixture
def sample_market_df():
    np.random.seed(42)
    n = 60
    close = 2450.0 + np.cumsum(np.random.randn(n) * 1.5)
    # create prominent wicks
    high = close + np.abs(np.random.randn(n) * 2.0)
    low = close - np.abs(np.random.randn(n) * 2.0)
    open_p = (high + low) / 2.0
    vol = np.random.randint(100, 2000, size=n).astype(float)

    return pd.DataFrame({
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": vol,
    })


def test_manipulation_index(sample_market_df):
    score, text = calculate_manipulation_index(sample_market_df)
    assert 1 <= score <= 10
    assert len(text) > 10


def test_zone_strength(sample_market_df):
    strength, text = calculate_zone_strength(sample_market_df)
    assert 0 <= strength <= 100
    assert len(text) > 10


def test_smf_ratio(sample_market_df):
    ratio, text = calculate_smf_ratio(sample_market_df)
    assert ratio >= 0.5
    assert len(text) > 10


def test_liquidity_grab(sample_market_df):
    score, text = calculate_liquidity_grab(sample_market_df)
    assert 1 <= score <= 10
    assert len(text) > 10


def test_delta_confidence(sample_market_df):
    level, text = calculate_delta_confidence(sample_market_df)
    assert level in ["LOW", "MEDIUM", "HIGH", "VERY HIGH"]
    assert len(text) > 10


def test_compute_institutional_metrics_and_formatting(sample_market_df):
    metrics = compute_institutional_metrics(sample_market_df)
    assert "manipulation_index" in metrics
    assert "zone_strength" in metrics
    assert "smf_ratio" in metrics
    assert "liquidity_grab" in metrics
    assert "delta_confidence" in metrics

    report = format_institutional_metrics_report(metrics)
    assert "Метрики по софту на текущий момент" in report
    assert "Manipulation Index:" in report
    assert "Zone Strength:" in report
    assert "SMF Ratio:" in report
    assert "Liquidity Grab:" in report
    assert "Delta Confidence:" in report
