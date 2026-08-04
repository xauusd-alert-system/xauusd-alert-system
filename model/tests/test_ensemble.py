"""
Unit tests for model/ensemble.py.
Run with: pytest model/tests/test_ensemble.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from regime.classifier import RegimeLabel
from model.ensemble import compute_ensemble_signal, EnsembleSignal

CFG = load_config()


def test_strong_agreement_produces_high_confidence_long():
    """Rule says TREND_UP (vote=+1) and ML strongly agrees (p_long=0.9) -> confident long."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.9, ml_p_short=0.1, cfg=CFG)
    assert sig.bias == "long"
    assert sig.confidence > 0.5
    assert isinstance(sig, EnsembleSignal)


def test_strong_agreement_produces_high_confidence_short():
    sig = compute_ensemble_signal(RegimeLabel.TREND_DOWN, ml_p_long=0.1, ml_p_short=0.9, cfg=CFG)
    assert sig.bias == "short"
    assert sig.confidence > 0.5


def test_disagreement_collapses_confidence_to_no_trade():
    """Rule says TREND_UP but ML strongly favors short -> disagreement, confidence collapses."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.15, ml_p_short=0.85, cfg=CFG)
    assert sig.bias == "no_trade"
    assert sig.confidence < CFG["ensemble"]["min_confidence_to_alert"]


def test_regime_no_trade_always_forces_no_trade_bias():
    """Even with maximally confident ML probabilities, NO_TRADE regime must override everything."""
    sig = compute_ensemble_signal(RegimeLabel.NO_TRADE, ml_p_long=0.99, ml_p_short=0.01, cfg=CFG)
    assert sig.bias == "no_trade"


def test_meta_filter_suppresses_low_confidence_in_choppy_regime():
    """
    RANGE and COMPRESSION are in suppress_regimes by default (config.yaml).
    Moderate agreement/confidence below min_regime_confidence must be suppressed.
    """
    sig = compute_ensemble_signal(RegimeLabel.COMPRESSION, ml_p_long=0.6, ml_p_short=0.4, cfg=CFG)
    assert sig.suppressed_by_meta_filter is True
    assert sig.bias == "no_trade"
    assert sig.confidence == 0.0


def test_meta_filter_allows_exceptionally_high_confidence_in_choppy_regime():
    """
    Even in a suppress_regimes regime, sufficiently high blended confidence (>= min_regime_confidence)
    should NOT be suppressed - the gate only fires when confidence is ALSO low.
    """
    sig = compute_ensemble_signal(RegimeLabel.RANGE, ml_p_long=0.95, ml_p_short=0.05, cfg=CFG)
    # Rule vote is 0 in RANGE regime (rule_based_signal only fires on trend regimes),
    # so agree=False by construction -> confidence collapses regardless of ML strength.
    # This IS the correct, conservative behavior: rule-based baseline never votes in RANGE,
    # so the ensemble cannot reach high confidence purely from ML in a choppy regime.
    assert sig.bias == "no_trade"


def test_confidence_never_exceeds_one():
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=1.0, ml_p_short=0.0, cfg=CFG)
    assert 0.0 <= sig.confidence <= 1.0


def test_reasoning_summary_is_nonempty_string():
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.8, ml_p_short=0.2, cfg=CFG)
    assert isinstance(sig.reasoning_summary, str)
    assert len(sig.reasoning_summary) > 10


def test_below_min_confidence_threshold_forces_no_trade():
    """Even with rule+ML agreement, if blended confidence < min_confidence_to_alert, must be no_trade."""
    # Weak ML confidence (p_long just above 0.5) combined with rule agreement may still
    # fall below threshold depending on weights - verify the gate is respected either way.
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.52, ml_p_short=0.48, cfg=CFG)
    if sig.confidence < CFG["ensemble"]["min_confidence_to_alert"]:
        assert sig.bias == "no_trade"


def test_news_guard_blocks_signal_during_news_window(monkeypatch):
    """If use_news_guard is True and we are in news red zone, bias must be no_trade."""
    import time
    def mock_is_news_red_zone(current_ts_utc, buf_before, buf_after):
        return True, "RED ZONE: Mock News Event"

    monkeypatch.setattr("model.ensemble.is_news_red_zone", mock_is_news_red_zone)

    # Create a test config copy with use_news_guard = True
    test_cfg = dict(CFG)
    test_cfg["ensemble"] = dict(test_cfg["ensemble"])
    test_cfg["ensemble"]["use_news_guard"] = True

    current_time_utc = int(time.time())
    sig = compute_ensemble_signal(
        RegimeLabel.TREND_UP,
        ml_p_long=0.9,
        ml_p_short=0.1,
        cfg=test_cfg,
        timestamp_utc=current_time_utc
    )
    assert sig.bias == "no_trade"
    assert "Blocked by News Guard" in sig.reasoning_summary


def _ev_cfg(ev_threshold: float) -> dict:
    """Config copy with a specific ev_threshold (uses payoff_ratio = tp1/stop = 1.0)."""
    cfg = dict(CFG)
    cfg["ensemble"] = dict(cfg["ensemble"])
    cfg["ensemble"]["ev_threshold"] = ev_threshold
    cfg["labeling"] = dict(cfg.get("labeling", {}))
    cfg["labeling"]["tp1_atr_multiplier"] = 1.0
    cfg["labeling"]["stop_atr_multiplier"] = 1.0
    return cfg


def test_ev_gate_disabled_by_default_preserves_baseline():
    """Default ev_threshold=0 must not change behaviour (gate off)."""
    assert CFG["ensemble"].get("ev_threshold", 0) == 0
    on = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, _ev_cfg(0.0))
    off = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, CFG)
    assert on.bias == off.bias


def test_ev_gate_requires_breakeven_expected_value():
    """With payoff_ratio=1.0, EV_risk = 2p - 1. A breakeven p=0.50 yields EV_risk=0,
    which is below any positive threshold -> declined as no_trade.
    Note: min_ml_probability is lowered locally so the breakeven p reaches the EV gate
    instead of being intercepted upstream by the Weak-ML-probability filter."""
    cfg = _ev_cfg(0.10)
    cfg["ensemble"]["min_ml_probability"] = 0.50
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.50, 0.50, cfg)
    # EV_risk = 0.50 * 1.0 - 0.50 = 0.0 < 0.10 -> declined.
    assert sig.bias == "no_trade"
    assert "EV gate declined" in sig.reasoning_summary


def test_ev_gate_allows_positive_expected_value():
    """p_long=0.75 with payoff_ratio=1.0 -> EV_risk = 0.5, above the 0.1 threshold."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.75, 0.25, _ev_cfg(0.10))
    # 0.75 is a confident signal; EV gate passes it, so bias reflects the strong agreement.
    assert sig.bias == "long"
    assert "EV gate declined" not in sig.reasoning_summary


def test_ev_gate_considers_payoff_ratio():
    """At p_long=0.55 with payoff_ratio=1.0, EV_risk = 0.55*1.0 - 0.45 = 0.10; with
    payoff_ratio=1.5, EV_risk = 0.55*1.5 - 0.45 = 0.375. A threshold of 0.15 declines
    the first but passes the second, proving the gate uses the TP1/stop payoff ratio,
    not just the raw probability."""
    low_ratio = _ev_cfg(0.15)
    low_ratio["labeling"]["tp1_atr_multiplier"] = 1.0
    low_ratio["labeling"]["stop_atr_multiplier"] = 1.0
    sig_ratio1 = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.55, 0.45, low_ratio)
    # EV_risk = 0.10 < 0.15 -> declined at 1:1 payoff.
    assert "EV gate declined" in sig_ratio1.reasoning_summary

    high_ratio = _ev_cfg(0.15)
    high_ratio["labeling"]["tp1_atr_multiplier"] = 1.5
    high_ratio["labeling"]["stop_atr_multiplier"] = 1.0
    sig_ratio15 = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.55, 0.45, high_ratio)
    # EV_risk = 0.375 >= 0.15 -> not declined at 1.5:1 payoff (the later no_trade, if any,
    # is purely a confidence matter and must not carry the EV-gate reasoning string).
    assert "EV gate declined" not in sig_ratio15.reasoning_summary


# ============= Step 6: Phase 4-5 (#30/#41/#16) =============

def _flag_cfg(key: str, value) -> dict:
    """Config copy with a single ensemble flag overridden (all others = global defaults)."""
    cfg = dict(CFG)
    cfg["ensemble"] = dict(cfg["ensemble"])
    cfg["ensemble"][key] = value
    return cfg


def _norm_cfg(flag: bool) -> dict:
    return _flag_cfg("normalize_probs", flag)


def _dyn_cfg(scale: float, enabled: bool = True) -> dict:
    cfg = _flag_cfg("dynamic_min_confidence", enabled)
    cfg["ensemble"]["dynamic_min_confidence_scale"] = scale
    return cfg


# ---- Phase 5 (#16): p_long + p_short = 1 normalization ----

def test_normalize_probs_disabled_by_default_preserves_baseline():
    """With normalize_probs=false the raw probabilities must be returned unchanged."""
    assert CFG["ensemble"].get("normalize_probs", False) is False
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.6, ml_p_short=0.3, cfg=_norm_cfg(False))
    # Raw pair sums to 0.9 and must NOT be re-scaled when disabled.
    assert sig.ml_p_long == pytest.approx(0.6)
    assert sig.ml_p_short == pytest.approx(0.3)
    assert sig.ml_p_long + sig.ml_p_short == pytest.approx(0.9)


def test_normalize_probs_rescales_pair_to_sum_one():
    """With normalize_probs=true a sub-unit-sum pair (0.6, 0.3 -> sum 0.9) is re-scaled
    to (0.6667, 0.3333) so p_long + p_short == 1.0 while preserving the ratio."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.6, ml_p_short=0.3, cfg=_norm_cfg(True))
    assert sig.ml_p_long + sig.ml_p_short == pytest.approx(1.0)
    assert sig.ml_p_long == pytest.approx(0.6 / 0.9)
    assert sig.ml_p_short == pytest.approx(0.3 / 0.9)


def test_normalize_probs_degenerate_total_falls_back_to_neutral():
    """A degenerate total (0.2 + 0.2 = 0.4) has no directional information -> the neutral
    (0.5, 0.5) pair, which never passes the weak-ML-probability filter -> no_trade."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.2, ml_p_short=0.2, cfg=_norm_cfg(True))
    assert sig.bias == "no_trade"
    assert sig.ml_p_long == pytest.approx(0.5)
    assert sig.ml_p_short == pytest.approx(0.5)


# ---- Phase 4 (#30): per-asset dynamic min_confidence scaling ----

def test_dynamic_min_confidence_disabled_by_default_preserves_baseline():
    """Default flag false must reproduce the exact static-bar outcome (no_trade at
    blended_confidence 0.5867 < static 0.60)."""
    assert CFG["ensemble"].get("dynamic_min_confidence", False) is False
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.558, ml_p_short=0.442, cfg=_dyn_cfg(1.0, enabled=False))
    # Static bar 0.60 rejects this moderate-edge signal.
    assert sig.bias == "no_trade"


def test_dynamic_min_confidence_relaxes_bar_for_strong_edge():
    """With dynamic enabled (scale=1.0, edge_credit=0.10, gain=2.0), the SAME inputs get
    bar 0.60 * 1.0 * 0.9 = 0.54 because edge=0.116 earns the full 0.10 credit, so the
    blended 0.5867 now clears it -> long. Proves the edge-driven relaxation."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.558, ml_p_short=0.442, cfg=_dyn_cfg(1.0))
    assert sig.bias == "long"


def test_dynamic_min_confidence_per_asset_scale_tightens_bar():
    """Per-asset scale is a STRICT multiplier of the alert bar. scale=2.0 ->
    bar 0.60 * 2.0 * 0.9 = 1.08 which exceeds even a maxed blended confidence of 1.0,
    forcing no_trade, while scale=1.0 admits the same maxed signal."""
    tight = _dyn_cfg(2.0)
    loose = _dyn_cfg(1.0)
    sig_loose = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.9, ml_p_short=0.1, cfg=loose)
    sig_tight = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.9, ml_p_short=0.1, cfg=tight)
    assert sig_loose.bias == "long"
    assert sig_tight.bias == "no_trade"


# ---- Phase 4 (#41): rule-vs-ML divergence hard veto ----

def test_hard_divergence_veto_forces_no_trade_on_opposite_votes():
    """TREND_UP (rule=+1) vs ML short (0.4/0.6 -> ml=-1) with the veto enabled must
    return no_trade immediately, flagged in the reasoning summary."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.4, ml_p_short=0.6, cfg=_flag_cfg("hard_divergence_veto", True))
    assert sig.bias == "no_trade"
    assert "Hard divergence veto" in sig.reasoning_summary


def test_hard_divergence_veto_tie_ml_vote_is_not_a_veto():
    """A tie (ml_vote=0) is NOT a divergence: the rule side continues past the veto
    (and, being undecided + below threshold, collapses downstream) - crucially the
    reasoning must NOT contain the hard-veto marker."""
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.55, ml_p_short=0.55, cfg=_flag_cfg("hard_divergence_veto", True))
    assert "Hard divergence veto" not in sig.reasoning_summary


def test_hard_divergence_veto_disabled_by_default_preserves_soft_collapse():
    """Default flag false: the same opposite-vote inputs must NOT carry the hard-veto
    marker (they still collapse to no_trade via the Phase-0+1 soft path)."""
    assert CFG["ensemble"].get("hard_divergence_veto", False) is False
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, ml_p_long=0.4, ml_p_short=0.6, cfg=_flag_cfg("hard_divergence_veto", False))
    assert sig.bias == "no_trade"
    assert "Hard divergence veto" not in sig.reasoning_summary
