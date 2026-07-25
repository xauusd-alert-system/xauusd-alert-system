"""
Ensemble layer: combines rule-based baseline signal + ML probability + a meta-filter
that suppresses signals during noisy/choppy regimes.

Design decision - three-stage pipeline:
  1. Rule-based directional vote (from regime/classifier.py::rule_based_signal logic)
  2. ML probability (from model/predictor.py::ModelPredictor)
  3. Meta-filter: regime-aware suppression gate that can force NO_TRADE regardless
     of what the first two stages say, if the current regime is in the configured
     suppress_regimes list (config.yaml model.meta_filter.suppress_regimes) AND the
     rule-based and ML signals disagree, or if ML confidence is below the floor.

Final ensemble confidence is a WEIGHTED BLEND (config.yaml ensemble.weight_rule_based /
weight_ml_probability), not a simple average - this lets us tune how much we trust
the rule baseline vs the learned model as more historical data accumulates.

CRITICAL: this module NEVER forces a directional bias when ensemble_confidence is
below ensemble.min_confidence_to_alert - it explicitly returns bias="no_trade" in
that case. This satisfies the project's hard constraint that the system must support
and default to a "no trade" state.
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from regime.classifier import RegimeLabel
from backtest.engine import rule_based_signal


@dataclass
class EnsembleSignal:
    bias: str          # "long", "short", or "no_trade"
    confidence: float  # 0.0 - 1.0, blended confidence score
    rule_vote: int      # -1, 0, +1
    ml_p_long: float
    ml_p_short: float
    regime: str
    suppressed_by_meta_filter: bool
    reasoning_summary: str


def _rule_confidence_component(rule_vote: int) -> float:
    """
    Rule-based confidence is binary by construction: it either has a clear directional
    vote (confidence=1.0 toward that direction) or no vote at all (confidence=0.0,
    treated as fully neutral/uncertain in the blend).
    """
    return 1.0 if rule_vote != 0 else 0.0


def compute_ensemble_signal(regime: RegimeLabel, ml_p_long: float, ml_p_short: float,
                             cfg: dict) -> EnsembleSignal:
    """
    Core ensemble logic - pure function, no side effects, fully deterministic given inputs.
    Called once per row/candle by realtime/pipeline.py and backtest strategy wrappers.
    """
    ens_cfg = cfg["ensemble"]
    meta_cfg = cfg["model"]["meta_filter"]

    rule_vote = rule_based_signal(regime)

    # ML directional vote and its own confidence (distance from 0.5, rescaled to [0,1])
    ml_vote = 1 if ml_p_long > ml_p_short else (-1 if ml_p_short > ml_p_long else 0)
    ml_confidence = abs(ml_p_long - 0.5) * 2  # 0.5->0 conf, 0.0 or 1.0 -> full conf

    rule_conf = _rule_confidence_component(rule_vote)

    # Weighted blend of confidences, direction-aware: only blend confidence towards
    # a direction if rule and ML AGREE on that direction: otherwise the disagreement
    # itself is a signal of uncertainty and confidence is heavily penalized.
    agree = (rule_vote == ml_vote) and rule_vote != 0

    if agree:
        blended_confidence = (ens_cfg["weight_rule_based"] * rule_conf +
                               ens_cfg["weight_ml_probability"] * ml_confidence)
        final_vote = rule_vote
    else:
        # Disagreement or one side neutral: confidence collapses toward the weaker signal.
        # This is intentional risk-aversion consistent with "fewer, higher-confidence alerts".
        blended_confidence = min(rule_conf, ml_confidence) * 0.5
        final_vote = ml_vote if ml_confidence > rule_conf else rule_vote

    # Meta-filter: suppress signals in known choppy/noisy regimes unless confidence is
    # exceptionally high (>= min_regime_confidence) - protects against overtrading ranges.
    suppressed = False
    if regime.value in meta_cfg["suppress_regimes"] and blended_confidence < meta_cfg["min_regime_confidence"]:
        suppressed = True
        blended_confidence = 0.0
        final_vote = 0

    # No-trade gates: insufficient confidence, neutral vote, or explicit regime no_trade
    if regime == RegimeLabel.NO_TRADE or final_vote == 0 or blended_confidence < ens_cfg["min_confidence_to_alert"]:
        bias = "no_trade"
    elif final_vote == 1:
        bias = "long"
    else:
        bias = "short"

    reasoning = (
        f"regime={regime.value}, rule_vote={rule_vote}, ml_p_long={ml_p_long:.3f}, "
        f"ml_p_short={ml_p_short:.3f}, agree={agree}, suppressed_by_meta_filter={suppressed}, "
        f"blended_confidence={blended_confidence:.3f}"
    )

    return EnsembleSignal(
        bias=bias,
        confidence=round(float(blended_confidence), 4),
        rule_vote=rule_vote,
        ml_p_long=float(ml_p_long),
        ml_p_short=float(ml_p_short),
        regime=regime.value,
        suppressed_by_meta_filter=suppressed,
        reasoning_summary=reasoning,
    )
