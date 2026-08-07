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
from config.loader import get_signal_grid

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
        except Exception as e:
            # Fail open (trading continues) if the news feed is unavailable, but
            # log it: a silently broken guard would let trades through during
            # high-impact news with no indication anything went wrong.
            logger.warning("News guard check failed; proceeding without news suppression: %s", e)

    is_crypto = "BTC" in asset_key.upper() or "ETH" in asset_key.upper()

    # Phase 5 (#16): enforce p_long + p_short = 1 BEFORE any downstream use.
    # Some predictors (e.g. a 3-class model) return p_long + p_short < 1 with the
    # residual mass on a "no_trade" class; raw probabilities that don't sum to 1
    # would under/over-state confidence and the EV gate. When normalize_probs is
    # enabled we re-scale the two directional masses so they sum to exactly 1.0.
    # A degenerate total (0.0 or NaN) carries no directional information, so we
    # fall back to the neutral (0.5, 0.5) pair which can never pass any filter.
    if ens_cfg.get("normalize_probs", False):
        try:
            p_long = float(ml_p_long)
            p_short = float(ml_p_short)
            total = p_long + p_short
            if total > 0.0 and not (p_long != p_long or p_short != p_short):
                ml_p_long = p_long / total
                ml_p_short = p_short / total
            else:
                ml_p_long = 0.5
                ml_p_short = 0.5
        except (TypeError, ValueError):
            # Non-numeric probabilities -> no directional information.
            ml_p_long = 0.5
            ml_p_short = 0.5

    ml_p_max = max(ml_p_long, ml_p_short)

    # 2. 🚨 УМНЫЙ ФИЛЬТР СЕССИЙ
    suppress_sessions = ens_cfg.get("suppress_sessions", ["asia", "off_session"])
    # Конфигурируемые пороги (HIGH 8): все «магические числа» вынесены в config.yaml.
    crypto_night_min_p = float(ens_cfg.get("crypto_night_min_probability", 0.58))
    required_p_min = float(ens_cfg.get("min_ml_probability", 0.55))
    # Масштаб приведения p_max к уверенности: 0.50 -> 0.0, 0.50+floor -> 1.0.
    ml_confidence_floor = float(ens_cfg.get("ml_confidence_floor", 0.62))
    ml_confidence_scale = max(1e-9, ml_confidence_floor - 0.50)

    if session and (session in suppress_sessions):
        if is_crypto and session == "asia":
            # Для Биткоина в Азию требуем СВЕРХ-ВЫСОКУЮ уверенность
            if ml_p_max < crypto_night_min_p:
                return EnsembleSignal(
                    bias="no_trade",
                    confidence=0.0,
                    rule_vote=0,
                    ml_p_long=float(ml_p_long),
                    ml_p_short=float(ml_p_short),
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    suppressed_by_meta_filter=True,
                    reasoning_summary=f"Crypto Night Mode: P={ml_p_max:.3f} below night threshold ({crypto_night_min_p})",
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

    # 3. Базовый порог для дневных сделок (P >= min_ml_probability)
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

    # Phase 2: EV-threshold entry gate. EV per unit risk over the TP3/stop ratio:
    #   EV_risk = p * payoff_ratio - (1 - p)
    # with p = directional probability (p_long for long, p_short for short) and
    # payoff_ratio = reward (TP3 distance) / risk (stop distance) from the
    # signal grid config (signal_grid.tp3_mult / stop_mult). Under the equal-step
    # spec this is 3/3 = 1.0 (risk:TP3 = 1:1), so the gate is a pure probability
    # quality filter: p must exceed 0.5 + threshold/2 to pass. Using TP1 here
    # would understate the execution payoff (TP1/SL = 1/3) and reject every
    # trade. A signal is declined if EV_risk < ev_threshold. ev_threshold=0
    # (default) disables the gate so the Phase-0+1 baseline is preserved unless
    # explicitly enabled.
    ev_threshold = float(ens_cfg.get("ev_threshold", 0.0))
    if ev_threshold > 0.0:
        reg_name = regime.value if hasattr(regime, "value") else str(regime)
        grid_cfg = get_signal_grid(cfg, regime=reg_name)
        tp3_mult = float(grid_cfg.get("tp3_mult", 3.0))
        stop_mult = float(grid_cfg.get("stop_mult", 3.0))
        payoff_ratio = (tp3_mult / stop_mult) if stop_mult > 0 else 1.0
        ev_risk_long = ml_p_long * payoff_ratio - (1.0 - ml_p_long)
        ev_risk_short = ml_p_short * payoff_ratio - (1.0 - ml_p_short)
        ev_risk_max = max(ev_risk_long, ev_risk_short)
        if ev_risk_max < ev_threshold:
            return EnsembleSignal(
                bias="no_trade",
                confidence=0.0,
                rule_vote=0,
                ml_p_long=float(ml_p_long),
                ml_p_short=float(ml_p_short),
                regime=regime.value if hasattr(regime, "value") else str(regime),
                suppressed_by_meta_filter=False,
                reasoning_summary=(
                    f"EV gate declined: EV_risk={ev_risk_max:.3f} < threshold={ev_threshold:.3f} "
                    f"(payoff_ratio={payoff_ratio:.3f})"
                ),
            )

    # Phase 4 (#30): per-asset dynamic min_confidence scaling when enabled.
    # base_min_confidence comes from the (already per-asset merged by the caller)
    # ensemble config. With dynamic_min_confidence=true the effective alert bar is
    #   effective_bar = base_bar * per_asset_scale * edge_factor
    # where per_asset_scale = dynamic_min_confidence_scale (per-asset, default 1.0)
    # lets each asset tighten/loosen its OWN bar, and
    #   edge_factor = 1 - min(edge_credit, edge * edge_gain)
    # relaxes the bar (up to edge_credit, default 0.10) as the normalized directional
    # edge |p_long - p_short| strengthens, so marginal weak-edge signals need more
    # evidence (fewer, better trades) while strong-edge signals are admitted.
    # Default false keeps the exact Phase-0+1 per-asset bar unchanged.
    min_confidence_to_alert = float(ens_cfg.get("min_confidence_to_alert", 0.65))
    if ens_cfg.get("dynamic_min_confidence", False):
        per_asset_scale = float(ens_cfg.get("dynamic_min_confidence_scale", 1.0))
        edge = abs(ml_p_long - ml_p_short)  # in [0, 1]; 0 = no directional edge
        edge_credit = float(ens_cfg.get("dynamic_edge_credit", 0.10))
        edge_gain = float(ens_cfg.get("dynamic_edge_gain", 2.0))
        edge_factor = 1.0 - min(edge_credit, edge * edge_gain)
        min_confidence_to_alert = min_confidence_to_alert * per_asset_scale * edge_factor

    min_regime_confidence = meta_cfg.get("min_regime_confidence", 0.65)
    suppress_regimes = meta_cfg.get("suppress_regimes", ["range", "compression", "reversal_watch"])
    # HIGH 8 / CRIT 4: веса читаются из ключей config.yaml rule_weight / ml_weight.
    weight_rule = float(ens_cfg.get("rule_weight", 0.20))
    weight_ml = float(ens_cfg.get("ml_weight", 0.80))

    if isinstance(regime, str):
        try:
            regime = RegimeLabel(regime)
        except ValueError:
            regime = RegimeLabel.NO_TRADE

    # Phase 4 (#41): rule-vs-ML divergence as a HARD VETO. When enabled, a non-zero
    # rule vote that OPPOSES the ML vote (rule says long, ML says short, or vice
    # versa) is treated as a hard no_trade, not merely a confidence collapse. The
    # asymmetric pairing (rule=+1, ml=0) is NOT a divergence - it just means the ML
    # is undecided, and the rule side may carry it (requires >= min_ml_probability
    # upstream). Default false keeps the Phase-0+1 soft-collapse behaviour.
    rule_vote = rule_based_signal(regime)
    ml_vote = 1 if ml_p_long > ml_p_short else (-1 if ml_p_short > ml_p_long else 0)
    rule_conf = _rule_confidence_component(rule_vote)

    if ens_cfg.get("hard_divergence_veto", False) and (rule_vote != 0) and (ml_vote != 0) and (rule_vote != ml_vote):
        return EnsembleSignal(
            bias="no_trade",
            confidence=0.0,
            rule_vote=rule_vote,
            ml_p_long=float(ml_p_long),
            ml_p_short=float(ml_p_short),
            regime=regime.value if hasattr(regime, "value") else str(regime),
            suppressed_by_meta_filter=False,
            reasoning_summary=(
                "Hard divergence veto (#41): rule_vote={rule_vote}, ml_vote={ml_vote} "
                "are opposite -> forced no_trade".format(rule_vote=rule_vote, ml_vote=ml_vote)
            ),
        )

    # Масштабирование: 0.50 -> 0.0, 0.50 + ml_confidence_scale -> 1.0
    ml_confidence = min(1.0, max(0.0, (ml_p_max - 0.50) / ml_confidence_scale))

    agree = (rule_vote == ml_vote) or (rule_vote == 0) or (ml_vote == 0)

    if agree:
        final_vote = rule_vote if rule_vote != 0 else ml_vote
        rule_comp = weight_rule * rule_conf if rule_vote != 0 else 0.0
        ml_comp = weight_ml * ml_confidence if ml_vote != 0 else 0.0
        blended_confidence = rule_comp + ml_comp
    else:
        blended_confidence = min(rule_conf, ml_confidence) * 0.3
        final_vote = ml_vote if ml_confidence > rule_conf else rule_vote

    suppressed = False
    regime_val = regime.value if hasattr(regime, "value") else str(regime)

    if (regime_val in suppress_regimes):
        if (rule_vote == 0) or (blended_confidence < min_regime_confidence):
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