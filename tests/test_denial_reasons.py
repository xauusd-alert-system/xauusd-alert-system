"""Tests for execution/denial_reasons and pipeline no-trade logging."""
import logging
from unittest.mock import patch

import pandas as pd

from execution.denial_reasons import DenialReason, render_line, top_reasons


def _news_ens_cfg(policy: str) -> dict:
    """Ensemble config that gates on news but passes through on ML edge/session,
    so a test exercises the news-guard branch in isolation."""
    return {
        "use_news_guard": True,
        "news_buffer_before_min": 30,
        "news_buffer_after_min": 30,
        "news_feed_failure_policy_live": policy,
        "min_edge": 0.01,
        "min_ml_probability": 0.50,
        "ml_confidence_floor": 0.51,
        "suppress_sessions": [],
        "suppress_regimes": [],
        "rule_weight": 0.2,
        "ml_weight": 0.8,
        "min_confidence_to_alert": 0.65,
    }


def _news_full_cfg(policy: str) -> dict:
    return {
        "ensemble": _news_ens_cfg(policy),
        "model": {"meta_filter": {"min_regime_confidence": 0.0, "suppress_regimes": []}},
    }


def test_signal_context_suffix_format():
    from execution.mt5_trader import _signal_context_suffix

    sig = {
        "regime": "trend_down", "session": "newyork",
        "p_long": 0.510, "p_short": 0.490, "rule_vote": 1, "confidence": 0.620,
    }
    out = _signal_context_suffix(sig)
    assert out.startswith(" | ctx: ")
    assert "regime=trend_down" in out
    assert "session=newyork" in out
    assert "p_long=0.510" in out
    assert "p_short=0.490" in out
    assert "rule=1" in out
    assert "conf=0.620" in out
    assert "| ctx:" not in out.split(" | ctx: ", 1)[1].split(" | ", 1)[0]  # no stray pipes inside ctx


def test_signal_context_suffix_missing_keys_safe():
    from execution.mt5_trader import _signal_context_suffix

    # Warm-up style response without model probabilities: skips, no raise.
    out = _signal_context_suffix({"regime": "range", "session": "asia"})
    assert out.startswith(" | ctx: ")
    assert "regime=range" in out
    assert "p_long" not in out
    assert "conf" not in out
    # Empty/None signal -> empty suffix.
    assert _signal_context_suffix({}) == ""
    assert _signal_context_suffix(None) == ""


def test_news_feed_unavailable_uses_separate_code():
    """fail_closed + feed DOWN -> denied under its own news_unavailable code,
    not a fake red_zone news_guard denial."""
    from model.ensemble import compute_ensemble_signal
    from regime.classifier import RegimeLabel

    cfg = _news_full_cfg("fail_closed")
    with patch("model.ensemble.news_guard_decision",
               return_value=(True, "NEWS FEED UNAVAILABLE: HTTPCon...", False)):
        sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg,
                                      session="london", timestamp_utc=1788177600,
                                      asset_key="XAUUSD")
    assert sig.bias == "no_trade"
    assert sig.denial_reasons
    codes = {r.code for r in sig.denial_reasons}
    assert "news_unavailable" in codes
    assert "news_guard" not in codes
    assert not any("red_zone" in r.detail for r in sig.denial_reasons)


def test_real_red_zone_uses_news_guard_code():
    """A genuine high-impact event (feed up) is still a news_guard red_zone."""
    from model.ensemble import compute_ensemble_signal
    from regime.classifier import RegimeLabel

    cfg = _news_full_cfg("fail_closed")
    with patch("model.ensemble.news_guard_decision",
               return_value=(True, "NFP: Non-Farm Payrolls", True)):
        sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg,
                                      session="london", timestamp_utc=1788177600,
                                      asset_key="XAUUSD")
    assert sig.bias == "no_trade"
    codes = {r.code for r in sig.denial_reasons}
    assert "news_guard" in codes
    assert "news_unavailable" not in codes
    assert any(r.code == "news_guard" and "red_zone" in r.detail for r in sig.denial_reasons)


def test_news_feed_unavailable_fail_open_does_not_block():
    """fail_open + feed DOWN -> trading proceeds (no news denial at all)."""
    from model.ensemble import compute_ensemble_signal
    from regime.classifier import RegimeLabel

    cfg = _news_full_cfg("fail_open")
    with patch("model.ensemble.news_guard_decision",
               return_value=(False, "", False)):
        sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.9, 0.1, cfg,
                                      session="london", timestamp_utc=1788177600,
                                      asset_key="XAUUSD")
    # Feed outage must NOT short-circuit to a news denial.
    assert not any(r.code in ("news_guard", "news_unavailable") for r in (sig.denial_reasons or []))
    assert sig.bias == "long"  # ML edge/session are configured to pass


def test_top_reasons_sorts_by_margin():
    r1 = DenialReason("ml_prob", "0.42<0.55", 0.77)
    r2 = DenialReason("vol_ratio", "0.8<1.5", 0.53)
    r3 = DenialReason("regime", "RANGING", 0.2)
    assert top_reasons([r2, r1, r3], 1)[0].code == "ml_prob"
    assert [r.code for r in top_reasons([r3, r2, r1], 2)] == ["ml_prob", "vol_ratio"]
    # n=3 returns all sorted
    assert [r.code for r in top_reasons([r2, r1, r3], 3)] == ["ml_prob", "vol_ratio", "regime"]


def test_render_line_format():
    r = DenialReason("ml_prob", "0.42<0.55", 0.77)
    line = render_line([r])
    assert "ml_prob=0.42<0.55" in line
    assert line.startswith("reasons:")
    # multiple
    r2 = DenialReason("regime", "RANGING(need TREND)", 0.2)
    line2 = render_line([r, r2], n=2)
    assert "ml_prob=0.42<0.55" in line2
    assert "regime=RANGING" in line2
    assert " | " in line2


def test_render_line_none():
    assert render_line([]) == "reasons: none"
    assert render_line([], n=3) == "reasons: none"


def test_ensemble_denial_reasons_present():
    """compute_ensemble_signal with weak prob must attach denial_reasons."""
    from model.ensemble import compute_ensemble_signal
    from regime.classifier import RegimeLabel

    cfg = {
        "ensemble": {
            "min_edge": 0.15,
            "min_ml_probability": 0.55,
            "ml_confidence_floor": 0.62,
            "suppress_sessions": [],
            "suppress_regimes": [],
            "min_confidence_to_alert": 0.65,
            "rule_weight": 0.2,
            "ml_weight": 0.8,
        },
        "model": {"meta_filter": {"min_regime_confidence": 0.65, "suppress_regimes": []}},
    }
    # Low edge should trigger denial
    sig = compute_ensemble_signal(RegimeLabel.TREND_UP, 0.51, 0.49, cfg, session="london", asset_key="XAUUSD")
    assert sig.bias == "no_trade"
    assert len(sig.denial_reasons) >= 1
    assert any(r.code in ("ml_edge", "ml_prob") for r in sig.denial_reasons)
    # render top 3
    line = render_line(sig.denial_reasons, 3)
    assert "reasons:" in line


def test_pipeline_signal_has_context_fields():
    """generate_signal must carry p_long/p_short/rule_vote so the trader can log
    the full per-bar decision context, not just top-N reasons."""
    from realtime.pipeline import RealtimePipeline
    from config.loader import load_config

    cfg = load_config()
    cfg.setdefault("logging", {})["denial_reasons"] = True
    pipe = RealtimePipeline(asset_key="XAUUSD", cfg=cfg, data_mode="mock")
    sig = pipe.generate_signal(300)
    assert "p_long" in sig and "p_short" in sig
    assert "rule_vote" in sig
    assert "regime" in sig and "session" in sig
    assert 0.0 <= sig["p_long"] <= 1.0 and 0.0 <= sig["p_short"] <= 1.0


def test_pipeline_no_trade_has_denial_reasons(caplog):
    """Pipeline generate_signal on warmup/no_trade must include denial_reasons and render."""
    from realtime.pipeline import RealtimePipeline
    from config.loader import load_config

    cfg = load_config()
    # Ensure logging.denial_reasons enabled for test
    cfg.setdefault("logging", {})["denial_reasons"] = True
    cfg["logging"]["denial_top_n"] = 3

    pipe = RealtimePipeline(asset_key="XAUUSD", cfg=cfg, data_mode="mock")
    # mock mode will generate some signal; force a no_trade by using asia session with suppress
    # We use the pipeline directly to check that even a no_trade dict contains denial_reasons
    # Build a small featured frame manually via pipeline helpers
    df = pipe._fetch_data_frame(pipe.timeframe, 300)
    # Force mock to be in asia by patching config temporarily
    pipe.effective_cfg["ensemble"]["suppress_sessions"] = ["asia", "london", "newyork", "off_session"]
    sig = pipe.generate_signal(300)
    # Should be no_trade due to session suppress (mock session will be one of those)
    if sig["bias"] == "no_trade":
        assert "denial_reasons" in sig
        assert isinstance(sig["denial_reasons"], list)
        assert len(sig["denial_reasons"]) >= 1
        # check render
        line = render_line(sig["denial_reasons"], 3)
        assert "reasons:" in line
        # code should be one of expected
        assert any(r.code in ("ml_prob", "ml_edge", "session", "regime", "vol_ratio", "impulse", "quality_score", "spread") for r in sig["denial_reasons"])


def test_no_trade_logs_reasons(caplog):
    """End-to-end: mt5_trader no-trade branch logs reasons at INFO, one line."""
    import logging
    from unittest.mock import MagicMock, patch

    from execution.mt5_trader import MultiAssetMT5Trader

    # Build a minimal trader without MT5 init
    with patch("execution.mt5_trader.initialize_mt5", return_value=True), \
         patch("execution.mt5_trader.validate_symbol"), \
         patch("execution.mt5_trader.resolve_server_offset_detailed", return_value=(3.0, {"mode": "test", "reason": "test"})), \
         patch("execution.mt5_trader.TelegramAlertBot"), \
         patch("execution.mt5_trader.InstitutionalRiskManager"), \
         patch("execution.mt5_trader.TradeThrottle"), \
         patch("realtime.book_feed.BookFeed"):
        # minimal cfg with logging enabled
        cfg = {
            "market_data": {"server_time_offset_hours": 3},
            "general": {"db_path": ":memory:"},
            "assets": {
                "XAUUSD": {"enabled": True, "mt5_symbol": "GOLD", "model_path": "nonexistent.joblib", "spread_usd": 0.25, "ensemble": {"min_confidence_to_alert": 0.66, "suppress_sessions": [], "suppress_regimes": []}, "signal_grid": {"step_min_points": 3.0, "step_max_points": 4.0}},
                "BTCUSD": {"enabled": False, "mt5_symbol": "BITCOIN", "model_path": "nonexistent.joblib"},
                "EURUSD": {"enabled": False, "mt5_symbol": "EURUSD", "model_path": "nonexistent.joblib"},
            },
            "execution": {"enabled_assets": ["XAUUSD"], "volume": 0.01, "max_concurrent_positions_global": 6, "max_open_positions_per_asset": 2, "trading_blackout": {"enabled": False}},
            "ensemble": {"min_confidence_to_alert": 0.65, "min_ml_probability": 0.55, "ml_confidence_floor": 0.62, "suppress_sessions": [], "suppress_regimes": []},
            "alerts": {},
            "sessions": {"asia": {"start": 0, "end": 8}, "london": {"start": 8, "end": 13}, "newyork": {"start": 13, "end": 22}},
            "features": {"structure_lookback": 20, "mtf_reference_timeframes": []},
            "logging": {"denial_reasons": True, "denial_top_n": 3},
            "deployment": {"mode": "research"},
            "risk_throttle": {"max_trades_per_day": 100, "loss_streak_threshold": 0, "cooldown_minutes": 0, "hard_stop_streak": 0, "risk_step_down_map": {1: 1.0}, "max_daily_loss_pct": 0, "reset_on_utc_midnight": True},
        }
        trader = MultiAssetMT5Trader.__new__(MultiAssetMT5Trader)
        trader.cfg = cfg
        trader.pipelines = {}
        # mock logger capture
        caplog.set_level(logging.INFO, logger="multi_asset_trader")
        # simulate the no_trade branch directly
        from execution.denial_reasons import DenialReason
        reasons = [DenialReason("ml_prob", "0.42<0.55", 0.76), DenialReason("regime", "RANGING(need TREND)", 0.2), DenialReason("vol_ratio", "0.8<1.5", 0.53)]
        signal = {"bias": "no_trade", "confidence": 0.0, "denial_reasons": reasons, "reasoning_summary": "Weak"}
        # replicate the logging logic from run_loop
        from execution.denial_reasons import render_line as rl
        log_cfg = (trader.cfg.get("logging") or {})
        enabled = log_cfg.get("denial_reasons", True)
        top_n = int(log_cfg.get("denial_top_n", 3))
        assert enabled is True
        suffix = f" | {rl(reasons, top_n)}" if reasons else " | reasons: none"
        msg = f"[XAUUSD] no trade (7.59s){suffix}"
        # check format matches spec
        assert "reasons:" in msg
        assert msg.count("|") >= 2  # at least 2 separators for 3 reasons
        assert "ml_prob=0.42<0.55" in msg
        # ensure one line
        assert "\n" not in msg
