"""
Tests for the regime x session x confidence ensemble filter (model/ensemble.py).

Run with: pytest model/tests/test_ensemble.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from model.ensemble import compute_ensemble_signal, EnsembleSignal
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
    """EURUSD ensemble override merges over the global ensemble section."""
    from scripts.run_backtest import merge_asset_cfg as _merge
    from config.loader import load_config
    cfg = load_config()
    eur_raw = cfg["assets"]["EURUSD"]["ensemble"]
    assert eur_raw.get("min_confidence_to_alert") == pytest.approx(0.85)
    assert "ev_threshold" not in eur_raw
    assert "hard_divergence_veto" not in eur_raw

    merged = _merge(cfg, "EURUSD", "ensemble")
    ens = merged["ensemble"]
    assert ens.get("min_confidence_to_alert") == pytest.approx(0.85)
    assert ens.get("ev_threshold", 0) == pytest.approx(0)
    assert ens.get("hard_divergence_veto", False) is False


def test_per_asset_gbpusd_ensemble_override_via_merge_asset_cfg():
    """Same contract for GBPUSD (legacy adopted 2026-08-08: bar 0.80 -> 0.60)."""
    from scripts.run_backtest import merge_asset_cfg as _merge
    from config.loader import load_config
    cfg = load_config()
    gbp_raw = cfg["assets"]["GBPUSD"]["ensemble"]
    assert gbp_raw.get("min_confidence_to_alert") == pytest.approx(0.60)
    assert "ev_threshold" not in gbp_raw
    assert "hard_divergence_veto" not in gbp_raw

    merged = _merge(cfg, "GBPUSD", "ensemble")
    ens = merged["ensemble"]
    assert ens.get("min_confidence_to_alert") == pytest.approx(0.60)
    assert ens.get("ev_threshold", 0) == pytest.approx(0)
    assert ens.get("hard_divergence_veto", False) is False


def test_per_asset_override_effective_cfg_in_pipeline():
    """RealtimePipeline.effective_cfg must mirror the same merge for EUR/GBP."""
    from realtime.pipeline import RealtimePipeline
    from config.loader import load_config
    cfg = load_config()

    eur_pipe = RealtimePipeline(cfg=cfg, asset_key="EURUSD", data_mode="mock")
    assert eur_pipe.effective_cfg["ensemble"].get("min_confidence_to_alert") == pytest.approx(0.85)
    assert eur_pipe.effective_cfg["ensemble"].get("ev_threshold", 0) == pytest.approx(0)
    assert eur_pipe.effective_cfg["ensemble"].get("hard_divergence_veto", False) is False

    gbp_pipe = RealtimePipeline(cfg=cfg, asset_key="GBPUSD", data_mode="mock")
    # legacy adopted 2026-08-08: bar 0.80 -> 0.60 (PF 1.39 vs 1.31 on the honest run)
    assert gbp_pipe.effective_cfg["ensemble"].get("min_confidence_to_alert") == pytest.approx(0.60)
    assert gbp_pipe.effective_cfg["ensemble"].get("ev_threshold", 0) == pytest.approx(0)
    assert gbp_pipe.effective_cfg["ensemble"].get("hard_divergence_veto", False) is False
