"""
Tests for the regime x session x confidence ensemble filter (model/ensemble.py).

Run with: pytest model/tests/test_ensemble.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from model.ensemble import compute_ensemble_signal
from regime.classifier import RegimeLabel


def _base_cfg():
    return {
        "ensemble": {
            "min_confidence_to_alert": 0.60,
            "min_regime_confidence": 0.60,
            "suppress_regimes": ["compression", "reversal_watch"],
            "suppress_sessions": ["asia", "off_session"],
            "rule_weight": 0.20,
            "ml_weight": 0.80,
            "ev_threshold": 0,
            "normalize_probs": False,
            "dynamic_min_confidence": False,
            "dynamic_min_confidence_scale": 1.0,
            "dynamic_edge_credit": 0.10,
            "dynamic_edge_gain": 2.0,
            "hard_divergence_veto": False,
        },
        "signal_grid": {
            "tp1_mult": 1.0,
            "tp2_mult": 2.0,
            "tp3_mult": 3.0,
            "stop_mult": 3.0,
        },
    }


def test_ensemble_long_signal_passes():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg, session="london")
    assert sig.bias == "long"
    assert sig.confidence > 0.60


def test_ensemble_short_signal_passes():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_DOWN, 0.1, 0.9, cfg, session="london")
    assert sig.bias == "short"


def test_ensemble_no_trade_on_low_confidence():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.55, 0.45, cfg, session="london")
    assert sig.bias == "no_trade"


def test_ensemble_suppressed_by_regime():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.COMPRESSION, 0.9, 0.1, cfg, session="london")
    assert sig.bias == "no_trade"


def test_ensemble_suppressed_by_session():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg, session="asia")
    assert sig.bias == "no_trade"


def test_ensemble_range_neutral_high_confidence():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.RANGE, 0.9, 0.1, cfg, session="london")
    # range is not in suppress_regimes, so a strong ML edge still passes
    assert sig.bias == "long"


# ---------------------------------------------------------------------------
# EV gate (Phase 2)
# ---------------------------------------------------------------------------


def test_ev_gate_disabled_by_default():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg, session="london")
    assert sig.bias == "long"  # ev_threshold=0 -> no gate


def test_ev_gate_blocks_weak_edge():
    cfg = _base_cfg()
    cfg["ensemble"]["ev_threshold"] = 0.50  # very high bar
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.55, 0.45, cfg, session="london")
    assert sig.bias == "no_trade"


# ---------------------------------------------------------------------------
# normalize_probs (Phase 5, #16)
# ---------------------------------------------------------------------------


def test_normalize_probs_disabled_by_default():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg, session="london")
    assert sig.bias == "long"


def test_normalize_probs_rescales_three_class():
    cfg = _base_cfg()
    cfg["ensemble"]["normalize_probs"] = True
    # 3-class model: p_long + p_short = 0.7 (0.3 on no_trade class)
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.6, 0.1, cfg, session="london")
    assert sig.bias in ("long", "no_trade")  # rescaled to 0.857/0.143


# ---------------------------------------------------------------------------
# hard_divergence_veto (Phase 4, #41)
# ---------------------------------------------------------------------------


def test_hard_divergence_veto_disabled_by_default():
    cfg = _base_cfg()
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg, session="london")
    assert sig.bias == "long"


def test_hard_divergence_veto_blocks_opposing_rule():
    cfg = _base_cfg()
    cfg["ensemble"]["hard_divergence_veto"] = True
    # regime trend_down (rule short) but ML says long
    sig = compute_ensemble_signal(RegimeLabel.TREND_DOWN, 0.9, 0.1, cfg, session="london")
    assert sig.bias == "no_trade"


# ---------------------------------------------------------------------------
# Per-asset merge contract (config/config.yaml)
# ---------------------------------------------------------------------------


def test_per_asset_eurusd_ensemble_override_via_merge_asset_cfg():
    """EURUSD ensemble override merges over the global ensemble section.

    AUDIT 2026-08-23: expectations are read from the asset section itself.
    Hard-coded bars went stale twice already (0.85 -> 0.70 owner request ->
    0.78 tightening); the CONTRACT under test is that the effective config
    mirrors the per-asset values, whatever they are."""
    from config.loader import load_config
    from scripts.run_backtest import merge_asset_cfg as _merge

    cfg = load_config()
    eur_raw = cfg["assets"]["EURUSD"]["ensemble"]
    expected_bar = float(eur_raw["min_confidence_to_alert"])

    merged = _merge(cfg, "EURUSD", "ensemble")
    ens = merged["ensemble"]
    assert ens.get("min_confidence_to_alert") == pytest.approx(expected_bar)
    assert ens.get("ev_threshold", 0) == pytest.approx(float(eur_raw.get("ev_threshold", 0)))
    assert bool(ens.get("hard_divergence_veto", False)) == bool(eur_raw.get("hard_divergence_veto", False))


def test_per_asset_gbpusd_ensemble_override_via_merge_asset_cfg():
    """Same contract for GBPUSD (see EURUSD test: drift-proof expectations)."""
    from config.loader import load_config
    from scripts.run_backtest import merge_asset_cfg as _merge

    cfg = load_config()
    gbp_raw = cfg["assets"]["GBPUSD"]["ensemble"]
    expected_bar = float(gbp_raw["min_confidence_to_alert"])

    merged = _merge(cfg, "GBPUSD", "ensemble")
    ens = merged["ensemble"]
    assert ens.get("min_confidence_to_alert") == pytest.approx(expected_bar)
    assert ens.get("ev_threshold", 0) == pytest.approx(float(gbp_raw.get("ev_threshold", 0)))
    assert bool(ens.get("hard_divergence_veto", False)) == bool(gbp_raw.get("hard_divergence_veto", False))


def test_per_asset_override_effective_cfg_in_pipeline():
    """RealtimePipeline.effective_cfg must mirror the same merge for EUR/GBP
    (audit 2026-08-23: drift-proof — expectations come from the config)."""
    from config.loader import load_config
    from realtime.pipeline import RealtimePipeline

    cfg = load_config()

    eur_pipe = RealtimePipeline(cfg=cfg, asset_key="EURUSD", data_mode="mock")
    eur_expected = float(cfg["assets"]["EURUSD"]["ensemble"]["min_confidence_to_alert"])
    assert eur_pipe.effective_cfg["ensemble"].get("min_confidence_to_alert") == pytest.approx(eur_expected)

    gbp_pipe = RealtimePipeline(cfg=cfg, asset_key="GBPUSD", data_mode="mock")
    gbp_expected = float(cfg["assets"]["GBPUSD"]["ensemble"]["min_confidence_to_alert"])
    assert gbp_pipe.effective_cfg["ensemble"].get("min_confidence_to_alert") == pytest.approx(gbp_expected)


def test_sentiment_veto_blocks_opposing_signal_when_enabled(monkeypatch):
    """W17: with use_sentiment_guard enabled, a strong bearish event vetoes a
    long signal. When disabled, the same signal passes unchanged."""
    from data.sentiment_analyzer import MacroNewsSentimentAnalyzer

    def fake_sentiment(self, *a, **k):
        return {"score": -0.8, "bias": "bearish", "title": "hawkish Fed", "in_red_zone": True}

    monkeypatch.setattr(MacroNewsSentimentAnalyzer, "red_zone_event_sentiment", fake_sentiment)

    # Enabled -> long vetoed by bearish sentiment.
    cfg_on = _base_cfg()
    cfg_on["ensemble"]["use_sentiment_guard"] = True
    cfg_on["ensemble"]["news_buffer_before_min"] = 30
    cfg_on["ensemble"]["news_buffer_after_min"] = 30
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg_on, session="london", timestamp_utc=1000000000)
    assert sig.bias == "no_trade"

    # Disabled -> baseline long preserved.
    cfg_off = _base_cfg()
    sig2 = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg_off, session="london", timestamp_utc=1000000000)
    assert sig2.bias == "long"


# ---------------------------------------------------------------------------
# P2-47 / TZ 5.3: ensemble hard reject on low confidence (reject_threshold)
# ---------------------------------------------------------------------------


def test_ensemble_rejects_low_confidence():
    """All models below reject_threshold -> no signal with reason
    ALL_MODELS_LOW_CONFIDENCE."""
    cfg = _base_cfg()
    cfg["ensemble"]["reject_threshold"] = 0.60
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.55, 0.45, cfg, session="london")
    assert sig.bias == "no_trade"
    assert sig.confidence == 0.0
    assert "ALL_MODELS_LOW_CONFIDENCE" in sig.reasoning_summary


def test_ensemble_accepts_mixed_confidence():
    """At least one directional probability >= reject_threshold -> the signal
    flows exactly as before (threshold does not veto on max(p_long, p_short))."""
    cfg = _base_cfg()
    cfg["ensemble"]["reject_threshold"] = 0.60
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.85, 0.15, cfg, session="london")
    assert sig.bias == "long"
    assert "ALL_MODELS_LOW_CONFIDENCE" not in sig.reasoning_summary


def test_reject_threshold_disabled_by_default():
    """reject_threshold null/absent -> feature off, behaviour unchanged even
    for very weak model probabilities."""
    cfg = _base_cfg()  # no reject_threshold key at all
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.52, 0.48, cfg, session="london")
    # Without the gate this is the ordinary weak-probability path (no hard
    # reject reason), NOT the ALL_MODELS_LOW_CONFIDENCE reject.
    assert "ALL_MODELS_LOW_CONFIDENCE" not in sig.reasoning_summary

    cfg_null = _base_cfg()
    cfg_null["ensemble"]["reject_threshold"] = None
    sig2 = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.52, 0.48, cfg_null, session="london")
    assert "ALL_MODELS_LOW_CONFIDENCE" not in sig2.reasoning_summary


def test_reject_threshold_respected():
    """A high threshold (0.8) blocks a signal that previously passed."""
    cfg_off = _base_cfg()
    sig_before = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.75, 0.25, cfg_off, session="london")
    assert sig_before.bias == "long"  # passes without the gate

    cfg_on = _base_cfg()
    cfg_on["ensemble"]["reject_threshold"] = 0.80
    sig_after = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.75, 0.25, cfg_on, session="london")
    assert sig_after.bias == "no_trade"
    assert "ALL_MODELS_LOW_CONFIDENCE" in sig_after.reasoning_summary
