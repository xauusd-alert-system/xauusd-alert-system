"""
Ensemble layer: combines rule-based baseline signal + ML probability + a meta-filter
that suppresses signals during noisy/choppy regimes and off-sessions.
Supports 24/7 Crypto Trading (BTCUSD) in Asia with Extra-High Confidence Threshold.
"""

import logging
from dataclasses import dataclass, field

from backtest.engine import rule_based_signal
from config.loader import get_signal_grid
from data.news_filter import news_guard_decision
from regime.classifier import RegimeLabel

logger = logging.getLogger("ensemble")

try:
    from execution.denial_reasons import DenialReason
except Exception:  # pragma: no cover
    DenialReason = None  # type: ignore


def _make_reason(code: str, detail: str, margin: float):
    if DenialReason is None:
        return None
    try:
        m = float(margin)
        if m != m:  # NaN
            m = 0.0
        # clamp to 0..1-ish but keep >1 for passed gates if needed
        if m < 0:
            m = 0.0
    except Exception:
        m = 0.0
    return DenialReason(code=code, detail=detail, margin=m)


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
    denial_reasons: list = field(default_factory=list)  # List[DenialReason]


def _rule_confidence_component(rule_vote: int) -> float:
    return 1.0 if rule_vote != 0 else 0.0


def bias_for_veto(vote: int) -> str:
    """Map an ensemble vote to a human-readable direction for reasoning text."""
    return "long" if vote == 1 else ("short" if vote == -1 else "no_trade")


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
            failure_policy = ens_cfg.get("news_feed_failure_policy_live", "fail_closed")
            in_red_zone, news_title, _feed_available = news_guard_decision(
                timestamp_utc,
                buf_before,
                buf_after,
                failure_policy=failure_policy,
                historical_calendar_path=ens_cfg.get("historical_news_calendar_path"),
            )
            if in_red_zone:
                # Distinguish a REAL high-impact red zone from a news-feed outage.
                # news_guard_decision already folds the failure policy into the
                # verdict: with fail_closed a feed outage returns blocked=True,
                # feed_available=False and news_title="NEWS FEED UNAVAILABLE: ...".
                # Tag that case separately (news_unavailable) so logs/analytics can
                # tell "suppressed by a genuine news event" from "the feed was just
                # down", instead of both surfacing as a red_zone denial.
                if not _feed_available:
                    r = _make_reason("news_unavailable", news_title[:40], 0.0)
                    reasoning = f"Blocked: news feed unavailable -> {news_title}"
                else:
                    r = _make_reason("news_guard", f"red_zone {news_title[:30]}", 0.0)
                    reasoning = f"Blocked by News Guard -> {news_title}"
                return EnsembleSignal(
                    bias="no_trade",
                    confidence=0.0,
                    rule_vote=0,
                    ml_p_long=float(ml_p_long),
                    ml_p_short=float(ml_p_short),
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    suppressed_by_meta_filter=True,
                    reasoning_summary=reasoning,
                    denial_reasons=[r] if r else [],
                )
        except Exception as e:
            logger.error("News guard check failed: %s", e)
            if ens_cfg.get("news_feed_failure_policy_live", "fail_closed") == "fail_closed":
                r = _make_reason("news_unavailable", f"feed_unavailable {str(e)[:24]}", 0.0)
                return EnsembleSignal(
                    bias="no_trade",
                    confidence=0.0,
                    rule_vote=0,
                    ml_p_long=float(ml_p_long),
                    ml_p_short=float(ml_p_short),
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    suppressed_by_meta_filter=True,
                    reasoning_summary=f"Blocked: news guard unavailable ({e})",
                    denial_reasons=[r] if r else [],
                )

    is_crypto = "BTC" in asset_key.upper() or "ETH" in asset_key.upper()

    # Phase 5 (#16): enforce p_long + p_short = 1 BEFORE any downstream use.
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
            ml_p_long = 0.5
            ml_p_short = 0.5

    ml_p_max = max(ml_p_long, ml_p_short)
    ml_edge = abs(ml_p_long - ml_p_short)  # directional edge

    # P2-47 / TZ 5.3: HARD REJECT on absolute model confidence. Unlike the
    # relative filters below (edge, blended confidence), this gate rejects the
    # signal when even the best directional probability is below an absolute
    # floor — the "all models ~0.5, averaging a weak signal" failure mode from
    # TZ Part 5. Configured via `ensemble.reject_threshold`; null/absent keeps
    # the exact previous behaviour (feature off by default for backwards
    # compatibility of the signal path).
    reject_threshold = ens_cfg.get("reject_threshold", None)
    if reject_threshold is not None:
        try:
            reject_threshold = float(reject_threshold)
        except (TypeError, ValueError):
            reject_threshold = None
    if reject_threshold is not None and ml_p_max < reject_threshold:
        return EnsembleSignal(
            bias="no_trade",
            confidence=0.0,
            rule_vote=0,
            ml_p_long=float(ml_p_long),
            ml_p_short=float(ml_p_short),
            regime=regime.value if hasattr(regime, "value") else str(regime),
            suppressed_by_meta_filter=True,
            reasoning_summary=(
                f"ALL_MODELS_LOW_CONFIDENCE: p_max={ml_p_max:.3f} < reject_threshold={reject_threshold:.3f}"
            ),
        )

    # CALIBRATION 2026-08-21: EDGE FILTER — reject low-conviction signals
    min_edge = float(ens_cfg.get("min_edge", 0.15))
    if ml_edge < min_edge:
        margin = (ml_edge / min_edge) if min_edge > 0 else 0.0
        r = _make_reason("ml_edge", f"{ml_edge:.3f}<{min_edge:.2f}", margin)
        reasons = [r] if r else []
        # secondary ml_prob only if also failing — otherwise pipeline will fill with vol/impulse
        req_p = float(ens_cfg.get("min_ml_probability", 0.55))
        if ml_p_max < req_p:
            r2 = _make_reason("ml_prob", f"{ml_p_max:.2f}<{req_p:.2f}", (ml_p_max / req_p) if req_p else 0.0)
            if r2:
                reasons.append(r2)
        return EnsembleSignal(
            bias="no_trade",
            confidence=0.0,
            rule_vote=0,
            ml_p_long=float(ml_p_long),
            ml_p_short=float(ml_p_short),
            regime=regime.value if hasattr(regime, "value") else str(regime),
            suppressed_by_meta_filter=True,
            reasoning_summary=f"Low edge: |p_long-p_short|={ml_edge:.3f} < min_edge={min_edge}",
            denial_reasons=reasons,
        )

    # 2. 🚨 УМНЫЙ ФИЛЬТР СЕССИЙ
    suppress_sessions = ens_cfg.get("suppress_sessions", ["asia", "off_session"])
    crypto_night_min_p = float(ens_cfg.get("crypto_night_min_probability", 0.58))
    required_p_min = float(ens_cfg.get("min_ml_probability", 0.55))
    ml_confidence_floor = float(ens_cfg.get("ml_confidence_floor", 0.62))
    ml_confidence_scale = max(1e-9, ml_confidence_floor - 0.50)

    if session and (session in suppress_sessions):
        if is_crypto and session == "asia":
            if ml_p_max < crypto_night_min_p:
                r = _make_reason("ml_prob", f"{ml_p_max:.2f}<{crypto_night_min_p:.2f}", (ml_p_max / crypto_night_min_p) if crypto_night_min_p else 0.0)
                r2 = _make_reason("session", f"{session}(need LDN/NY)", 0.0)
                return EnsembleSignal(
                    bias="no_trade",
                    confidence=0.0,
                    rule_vote=0,
                    ml_p_long=float(ml_p_long),
                    ml_p_short=float(ml_p_short),
                    regime=regime.value if hasattr(regime, "value") else str(regime),
                    suppressed_by_meta_filter=True,
                    reasoning_summary=f"Crypto Night Mode: P={ml_p_max:.3f} below night threshold ({crypto_night_min_p})",
                    denial_reasons=[x for x in [r, r2] if x],
                )
        else:
            r = _make_reason("session", f"{session}(need LDN/NY)", 0.0)
            return EnsembleSignal(
                bias="no_trade",
                confidence=0.0,
                rule_vote=0,
                ml_p_long=float(ml_p_long),
                ml_p_short=float(ml_p_short),
                regime=regime.value if hasattr(regime, "value") else str(regime),
                suppressed_by_meta_filter=True,
                reasoning_summary=f"Suppressed by session filter ({session})",
                denial_reasons=[r] if r else [],
            )

    # 3. Базовый порог для дневных сделок (P >= min_ml_probability)
    if ml_p_max < required_p_min:
        r = _make_reason("ml_prob", f"{ml_p_max:.2f}<{required_p_min:.2f}", (ml_p_max / required_p_min) if required_p_min else 0.0)
        return EnsembleSignal(
            bias="no_trade",
            confidence=0.0,
            rule_vote=0,
            ml_p_long=float(ml_p_long),
            ml_p_short=float(ml_p_short),
            regime=regime.value if hasattr(regime, "value") else str(regime),
            suppressed_by_meta_filter=False,
            reasoning_summary=f"Weak ML probability (p_max={ml_p_max:.3f} < {required_p_min})",
            denial_reasons=[r] if r else [],
        )

    # Phase 2: EV-threshold entry gate.
    ev_threshold = float(ens_cfg.get("ev_threshold", 0.0))
    if ev_threshold > 0.0:
        reg_name = regime.value if hasattr(regime, "value") else str(regime)
        asset_cfg = cfg.get("assets", {}).get(asset_key, {})
        grid_cfg = get_signal_grid(cfg, asset_cfg, regime=reg_name)
        tp3_mult = float(grid_cfg.get("tp3_mult", 3.0))
        stop_mult = float(grid_cfg.get("stop_mult", 3.0))
        payoff_ratio = (tp3_mult / stop_mult) if stop_mult > 0 else 1.0
        ev_risk_long = ml_p_long * payoff_ratio - (1.0 - ml_p_long)
        ev_risk_short = ml_p_short * payoff_ratio - (1.0 - ml_p_short)
        ev_risk_max = max(ev_risk_long, ev_risk_short)
        if ev_risk_max < ev_threshold:
            margin = (ev_risk_max / ev_threshold) if ev_threshold else 0.0
            r = _make_reason("ev_gate", f"{ev_risk_max:.2f}<{ev_threshold:.2f}", margin)
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
                denial_reasons=[r] if r else [],
            )

    # Phase 4 (#30): per-asset dynamic min_confidence scaling
    min_confidence_to_alert = float(ens_cfg.get("min_confidence_to_alert", 0.65))
    if ens_cfg.get("dynamic_min_confidence", False):
        per_asset_scale = float(ens_cfg.get("dynamic_min_confidence_scale", 1.0))
        edge = abs(ml_p_long - ml_p_short)
        edge_credit = float(ens_cfg.get("dynamic_edge_credit", 0.10))
        edge_gain = float(ens_cfg.get("dynamic_edge_gain", 2.0))
        edge_factor = 1.0 - min(edge_credit, edge * edge_gain)
        min_confidence_to_alert = min_confidence_to_alert * per_asset_scale * edge_factor

    min_regime_confidence = meta_cfg.get("min_regime_confidence", 0.65)
    suppress_regimes = meta_cfg.get("suppress_regimes", ["range", "compression", "reversal_watch"])
    weight_rule = float(ens_cfg.get("rule_weight", 0.20))
    weight_ml = float(ens_cfg.get("ml_weight", 0.80))

    if isinstance(regime, str):
        try:
            regime = RegimeLabel(regime)
        except ValueError:
            regime = RegimeLabel.NO_TRADE

    # Phase 4 (#41): hard divergence veto
    rule_vote = rule_based_signal(regime)
    ml_vote = 1 if ml_p_long > ml_p_short else (-1 if ml_p_short > ml_p_long else 0)
    rule_conf = _rule_confidence_component(rule_vote)

    if ens_cfg.get("hard_divergence_veto", False) and (rule_vote != 0) and (ml_vote != 0) and (rule_vote != ml_vote):
        r = _make_reason("divergence", f"rule={rule_vote} vs ml={ml_vote}", 0.0)
        return EnsembleSignal(
            bias="no_trade",
            confidence=0.0,
            rule_vote=rule_vote,
            ml_p_long=float(ml_p_long),
            ml_p_short=float(ml_p_short),
            regime=regime.value if hasattr(regime, "value") else str(regime),
            suppressed_by_meta_filter=False,
            reasoning_summary=(
                f"Hard divergence veto (#41): rule_vote={rule_vote}, ml_vote={ml_vote} are opposite -> forced no_trade"
            ),
            denial_reasons=[r] if r else [],
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
        if ml_confidence >= 0.70:
            blended_confidence = weight_ml * ml_confidence + weight_rule * rule_conf * 0.2
            final_vote = ml_vote
        else:
            blended_confidence = min(rule_conf, ml_confidence) * 0.3
            final_vote = ml_vote if ml_confidence > rule_conf else rule_vote

    suppressed = False
    regime_val = regime.value if hasattr(regime, "value") else str(regime)

    if regime_val in suppress_regimes:
        if (rule_vote == 0) or (blended_confidence < min_regime_confidence):
            # regime suppress — closest to passing when blended is close to threshold
            margin = (blended_confidence / min_regime_confidence) if min_regime_confidence else 0.0
            r = _make_reason("regime", f"{regime_val}(need TREND)", margin)
            r2 = _make_reason("quality_score", f"{blended_confidence:.2f}<{min_regime_confidence:.2f}", margin)
            return EnsembleSignal(
                bias="no_trade",
                confidence=0.0,
                rule_vote=rule_vote,
                ml_p_long=float(ml_p_long),
                ml_p_short=float(ml_p_short),
                regime=regime_val,
                suppressed_by_meta_filter=True,
                reasoning_summary=f"Suppressed by regime filter ({regime_val}) blended {blended_confidence:.3f} < {min_regime_confidence}",
                denial_reasons=[x for x in [r, r2] if x],
            )

    # W17 sentiment veto
    if ens_cfg.get("use_sentiment_guard", False) and final_vote != 0:
        try:
            from data.sentiment_analyzer import MacroNewsSentimentAnalyzer

            veto_threshold = float(ens_cfg.get("sentiment_veto_threshold", 0.5))
            if timestamp_utc is not None:
                sent = MacroNewsSentimentAnalyzer().red_zone_event_sentiment(
                    timestamp_utc,
                    buffer_before_minutes=int(ens_cfg.get("news_buffer_before_min", 30)),
                    buffer_after_minutes=int(ens_cfg.get("news_buffer_after_min", 30)),
                )
                if sent["in_red_zone"] and abs(sent["score"]) >= veto_threshold:
                    # sentiment positive -> against a short; negative -> against a long
                    sentiment_opposes = (final_vote == 1 and sent["score"] < 0) or (
                        final_vote == -1 and sent["score"] > 0
                    )
                    if sentiment_opposes:
                        r = _make_reason("sentiment", f"{sent['score']:+.2f} opposes {bias_for_veto(final_vote)}", 0.0)
                        return EnsembleSignal(
                            bias="no_trade",
                            confidence=0.0,
                            rule_vote=rule_vote,
                            ml_p_long=float(ml_p_long),
                            ml_p_short=float(ml_p_short),
                            regime=regime_val,
                            suppressed_by_meta_filter=True,
                            reasoning_summary=(
                                f"Sentiment veto: event '{sent['title']}' sentiment "
                                f"{sent['bias']} ({sent['score']:+.2f}) opposes {bias_for_veto(final_vote)}"
                            ),
                            denial_reasons=[r] if r else [],
                        )
        except Exception as e:
            logger.warning("Sentiment guard check failed; proceeding without veto: %s", e)

    if (regime == RegimeLabel.NO_TRADE) or (final_vote == 0) or (blended_confidence < min_confidence_to_alert):
        # final confidence gate
        margin = (blended_confidence / min_confidence_to_alert) if min_confidence_to_alert else 0.0
        reasons = []
        r = _make_reason("quality_score", f"{blended_confidence:.2f}<{min_confidence_to_alert:.2f}", margin)
        if r:
            reasons.append(r)
        # regime context if no_trade regime
        if regime == RegimeLabel.NO_TRADE:
            r3 = _make_reason("regime", f"{regime_val}(no_trade)", 0.0)
            if r3:
                reasons.append(r3)
        elif final_vote == 0:
            r3 = _make_reason("ml_edge", f"{ml_edge:.2f}<{min_edge:.2f}", (ml_edge / min_edge) if min_edge else 0.0)
            if r3:
                reasons.append(r3)
        bias = "no_trade"
    elif final_vote == 1:
        bias = "long"
        reasons = []
    else:
        bias = "short"
        reasons = []

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
        denial_reasons=reasons if bias == "no_trade" else [],
    )
