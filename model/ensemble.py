"""
Ensemble layer: combines rule-based baseline signal + ML probability + a meta-filter
that suppresses signals during noisy/choppy regimes and off-sessions.
Supports 24/7 Crypto Trading (BTCUSD) in Asia with Extra-High Confidence Threshold.
"""
import logging
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from regime.classifier import RegimeLabel
from backtest.engine import rule_based_signal
from data.news_filter import is_news_red_zone

logger = logging.getLogger("ensemble")


@dataclass
class EnsembleSignal:
    bias: str  # "long", "short", or "no_trade"
    confidence: float  # 0.0 - 1.0, blended confidence score
    rule_vote: int  # -1, 0, +1
    ml_p_long: float
    ml_p_short: float
    regime: str
    suppressed_by_meta_filter: bool
    reasoning_summary: str


def _rule_confidence_component(rule_vote: int) -> float:
    return 1.0 if rule_vote != 0 else 0.0


def compute_ensemble_signal(
    regime: RegimeLabel,
    ml_p_long: float,
    ml_p_short: float,
    cfg: dict,
    session: str = None,
    timestamp_utc: int = None,
    asset_key: str = "XAUUSD",
) -> EnsembleSignal:
    """
    Core ensemble logic with Asset-Aware Session Filtering.
    BTCUSD can trade in Asia Session with elevated confidence threshold (P >= 0.58).
    """
    ens_cfg = cfg.get("ensemble", {})
    model_cfg = cfg.get("model", {})
    meta_cfg = model_cfg.get("meta_filter", ens_cfg)

    # 1. 🚨 НОВОСТНОЙ ЗАЩИТНИК (NEWS GUARD)
    if timestamp_utc is not None and ens_cfg.get("use_news_guard", False):
        buf_before = ens_cfg.get("news_buffer_before_min", 30)
        buf_after = ens_cfg.get("news_buffer_after_min", 30)
        try:
            in_red_zone, news_title = is_news_red_zone(timestamp_utc, buf_before, buf_after)
            if in_red_zone:
                return EnsembleSignal(
                    bias="no_trade",
                    confidence=0.0,
                    rule_vote=0,
                    ml_p_long=float(ml_p_long),
                    ml_p_short=float(ml_p_short),
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    suppressed_by_meta_filter=True,
                    reasoning_summary=f"Blocked by News Guard -> {news_title}",
                )
        except Exception:
            pass

    is_crypto = "BTC" in asset_key.upper() or "ETH" in asset_key.upper()
    ml_p_max = max(ml_p_long, ml_p_short)

    # 2. 🚨 УМНЫЙ ФИЛЬТР СЕССИЙ
    suppress_sessions = ens_cfg.get("suppress_sessions", ["asia", "off_session"])
    
    if session and (session in suppress_sessions):
        if is_crypto and session == "asia":
            # Для Биткоина в Азию требуем СВЕРХ-ВЫСОКУЮ уверенность P >= 0.58
            if ml_p_max < 0.58:
                return EnsembleSignal(
                    bias="no_trade",
                    confidence=0.0,
                    rule_vote=0,
                    ml_p_long=float(ml_p_long),
                    ml_p_short=float(ml_p_short),
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    suppressed_by_meta_filter=True,
                    reasoning_summary=f"Crypto Night Mode: P={ml_p_max:.3f} below night threshold (0.58)",
                )
        else:
            return EnsembleSignal(
                bias="no_trade",
                confidence=0.0,
                rule_vote=0,
                ml_p_long=float(ml_p_long),
                ml_p_short=float(ml_p_short),
                regime=regime.value if hasattr(regime, "value") else str(regime),
                suppressed_by_meta_filter=True,
                reasoning_summary=f"Suppressed by session filter ({session})",
            )

    # 3. Базовый порог для дневных сделок (P >= 0.55)
    required_p_min = 0.55
    if ml_p_max < required_p_min:
        return EnsembleSignal(
            bias="no_trade",
            confidence=0.0,
            rule_vote=0,
            ml_p_long=float(ml_p_long),
            ml_p_short=float(ml_p_short),
            regime=regime.value if hasattr(regime, "value") else str(regime),
            suppressed_by_meta_filter=False,
            reasoning_summary=f"Weak ML probability (p_max={ml_p_max:.3f} < {required_p_min})",
        )

    min_confidence_to_alert = ens_cfg.get("min_confidence_to_alert", 0.65)
    min_regime_confidence = meta_cfg.get("min_regime_confidence", 0.65)
    suppress_regimes = meta_cfg.get("suppress_regimes", ["range", "compression", "reversal_watch"])
    weight_rule = ens_cfg.get("weight_rule_based", 0.20)
    weight_ml = ens_cfg.get("weight_ml_probability", 0.80)

    if isinstance(regime, str):
        try:
            regime = RegimeLabel(regime)
        except ValueError:
            regime = RegimeLabel.NO_TRADE

    rule_vote = rule_based_signal(regime)
    ml_vote = 1 if ml_p_long > ml_p_short else (-1 if ml_p_short > ml_p_long else 0)

    # Масштабирование: 0.50 -> 0.0, 0.62 -> 1.0
    ml_confidence = min(1.0, max(0.0, (ml_p_max - 0.50) / 0.12))

    rule_conf = _rule_confidence_component(rule_vote)
    agree = (rule_vote == ml_vote) and (rule_vote != 0)

    if agree:
        blended_confidence = (weight_rule * rule_conf) + (weight_ml * ml_confidence)
        final_vote = rule_vote
    else:
        blended_confidence = min(rule_conf, ml_confidence) * 0.3
        final_vote = ml_vote if ml_confidence > rule_conf else rule_vote

    suppressed = False
    regime_val = regime.value if hasattr(regime, "value") else str(regime)

    if (regime_val in suppress_regimes) and (blended_confidence < min_regime_confidence):
        suppressed = True
        blended_confidence = 0.0
        final_vote = 0

    if (regime == RegimeLabel.NO_TRADE) or (final_vote == 0) or (blended_confidence < min_confidence_to_alert):
        bias = "no_trade"
    elif final_vote == 1:
        bias = "long"
    else:
        bias = "short"

    reasoning = (
        f"regime={regime_val}, session={session}, rule_vote={rule_vote}, ml_p_long={ml_p_long:.3f}, "
        f"ml_p_short={ml_p_short:.3f}, agree={agree}, blended_confidence={blended_confidence:.3f}"
    )

    return EnsembleSignal(
        bias=bias,
        confidence=round(float(blended_confidence), 4),
        rule_vote=rule_vote,
        ml_p_long=float(ml_p_long),
        ml_p_short=float(ml_p_short),
        regime=regime_val,
        suppressed_by_meta_filter=suppressed,
        reasoning_summary=reasoning,
    )