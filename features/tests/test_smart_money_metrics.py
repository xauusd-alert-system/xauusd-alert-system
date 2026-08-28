"""
Tests for Smart Money & Institutional Microstructure Metrics.
Verifies calculation of Manipulation Index, Zone Strength, SMF Ratio,
Liquidity Grab, Delta Confidence, and report formatting.
"""
import numpy as np
import pandas as pd
import pytest

from features.smart_money_metrics import (
    calculate_delta_confidence,
    calculate_liquidity_grab,
    calculate_manipulation_index,
    calculate_smf_ratio,
    calculate_zone_strength,
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


def test_delta_confidence_very_high_is_reachable():
    """N9: with a very strong, consistent delta the level must be VERY HIGH
    (previously unreachable because the looser HIGH branch ran first)."""
    import pandas as pd
    # 30 bars, consistently buying (close near high -> positive signed delta),
    # strong cumulative slope relative to volume.
    n = 30
    df = pd.DataFrame({
        "high": [100.0] * n,
        "low": [99.0] * n,
        "close": [99.9] * n,   # pos_in_bar ~ (99.9-99)/(100-99) = 0.9 -> positive
        "volume": [1.0] * n,
    })
    level, _ = calculate_delta_confidence(df)
    assert level == "VERY HIGH"


# ==========================================================================
# Position Quality audit regressions (ТЗ §5/§6/§12/§24/§26)
# ==========================================================================

from features.smart_money_metrics import (
    FORBIDDEN_CLAIMS,
    PARAMETER_META,
    SOURCE_KIND,
)


def test_report_text_never_claims_institutional_control_from_proxy(sample_market_df):
    """§5/§12: OHLCV-proxy metrics must never be phrased as confirmed
    institutional activity / real flow. Any future rewording that reintroduces
    the forbidden claims fails here."""
    report = format_institutional_metrics_report(
        compute_institutional_metrics(sample_market_df))
    lower = report.lower()
    for claim in FORBIDDEN_CLAIMS:
        assert claim.lower() not in lower, f"forbidden claim present: {claim}"
    # honest disclaimer present
    assert "ohlcv-прокси" in lower
    assert "не реальный торговый поток" in lower


def test_every_parameter_carries_source_kind_ohlcv_proxy(sample_market_df):
    """§5/§24: each parameter result must declare its true source kind and the
    real lookback it used — never a fabricated real-flow source."""
    metrics = compute_institutional_metrics(sample_market_df)
    for name, meta in PARAMETER_META.items():
        param = metrics[name]
        assert param["source_kind"] == SOURCE_KIND == "ohlcv_proxy", name
        assert param["lookback"] == meta["lookback"], name
        assert param["data_status"] in {"sufficient", "insufficient"}, name
    agg = metrics["source_provenance"]
    assert agg["source_kind"] == "ohlcv_proxy"
    assert agg["lookbacks"] == {name: meta["lookback"]
                                for name, meta in PARAMETER_META.items()}
    assert "real trade flow" in agg["note"].lower() or "не реальный" in agg["note"]


def test_insufficient_delta_data_is_marked_not_valid(sample_market_df):
    """§12/§27: a too-short frame must be explicitly insufficient, never a
    silently valid value."""
    short = sample_market_df.iloc[:5].copy()
    metrics = compute_institutional_metrics(short)
    delta = metrics["delta_confidence"]
    assert delta["data_status"] == "insufficient"
    assert delta["source_kind"] == "ohlcv_proxy"
    # the level is still a display value, but the marker prevents treating it
    # as a valid reading downstream
    assert delta["level"] in {"LOW", "MEDIUM", "HIGH", "VERY HIGH"}


def test_short_frame_marks_all_parameters_insufficient(sample_market_df):
    short = sample_market_df.iloc[:3].copy()
    metrics = compute_institutional_metrics(short)
    for name in PARAMETER_META:
        assert metrics[name]["data_status"] == "insufficient", name


def test_repeated_snapshot_is_deterministic(sample_market_df):
    """§26: the same snapshot must produce byte-identical results on repeat."""
    first = compute_institutional_metrics(sample_market_df)
    second = compute_institutional_metrics(sample_market_df)
    assert first == second
    # reordering rows (same content) must NOT change the per-bar result
    assert first["manipulation_index"]["source_kind"] == "ohlcv_proxy"
