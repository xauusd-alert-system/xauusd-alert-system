"""
Realtime inference pipeline: wires MT5 live data -> features -> regime -> model -> ensemble
into a single callable that produces the structured signal JSON required by the
FastAPI service, MT5 Auto-Trader, and Telegram bot.
"""
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
import pandas as pd

from config.loader import (
    load_config,
    get_signal_grid,
    effective_asset_config,
    resolve_signal_step as _resolve_signal_step,
)
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from features.structure import detect_structure
from features.mtf_confluence import compute_confluence_score
from features.order_flow import add_order_flow_features
from regime.classifier import add_regime_indicators, classify_regime_series, RegimeLabel
from model.predictor import ModelPredictor
from model.ensemble import compute_ensemble_signal
from data.session_tagger import tag_dataframe
from config.strategy_contract import strategy_identity

logger = logging.getLogger("realtime_pipeline")


def resolve_signal_step(atr_val: float, grid_cfg: dict) -> float:
    """
    Resolves the equal-step TP/SL grid step for a signal.

    Priority: fixed `step_points` (price points) when set, otherwise the
    dynamic signal-bar ATR step (1.0 * ATR). TP multipliers are applied once
    after step resolution. The result is
    clamped to [step_min_points, step_max_points] when those are configured.
    """
    return _resolve_signal_step(atr_val, grid_cfg)


class RealtimePipeline:
    def __init__(
        self,
        cfg: dict = None,
        model_path: str = None,
        asset_key: str = None,
        data_mode: str = "live",
        book_feed=None,
    ):
        self.cfg = cfg or load_config()
        self.data_mode = data_mode
        self.book_feed = book_feed

        # asset_key may be passed explicitly or inferred from model_path.
        if asset_key is None:
            if model_path:
                for key, acfg in self.cfg.get("assets", {}).items():
                    if acfg.get("model_path") == model_path:
                        asset_key = key
                        break
            if asset_key is None:
                asset_key = "XAUUSD"
        self.asset_key = asset_key

        assets = self.cfg["assets"]
        if asset_key not in assets:
            raise ValueError(f"Unknown asset: {asset_key}")

        self.asset_cfg = assets[asset_key]
        self.mt5_symbol = self.asset_cfg.get("mt5_symbol", "GOLD")
        # Explicit model_path (env/config) takes precedence over per-asset default.
        self.model_path = model_path or self.asset_cfg.get("model_path")
        # Per-asset timeframe override (assets.<key>.timeframe), else global.
        self.timeframe = self.asset_cfg.get("timeframe") or self.cfg.get(
            "market_data", {}
        ).get("timeframe", "M5")
        self._predictor = ModelPredictor(self.model_path) if self.model_path and os.path.exists(self.model_path) else None

        # One resolver for production training, research and live inference.
        self.effective_cfg = effective_asset_config(self.cfg, asset_key)
        model_metadata = self._predictor.metadata if self._predictor is not None else {}
        self.strategy_identity = strategy_identity(self.effective_cfg, model_metadata)
        if self.model_path and os.path.isfile(self.model_path):
            digest = hashlib.sha256()
            with open(self.model_path, "rb") as model_file:
                for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            self.strategy_identity["model_hash"] = digest.hexdigest()

    def get_frame(self, n_candles: int = 100, build_features: bool = False) -> pd.DataFrame:
        """Return a raw (or feature-built) DataFrame of the asset's real candles.

        Owner request 2026-08-11: lets /metrics and the web dashboard compute
        SMC/institutional metrics on REAL market data (via the live pipeline)
        instead of only the synthetic simulator. build_features=False returns the
        tagged OHLCV frame (sufficient for microstructure metrics); True runs the
        full feature build.
        """
        df = self._fetch_data_frame(self.timeframe, n_candles)
        if build_features:
            return self._build_features(df)
        return df

    def _fetch_data_frame(self, timeframe: str, n_candles: int) -> pd.DataFrame:
        """Fetches and prepares DataFrame directly from MT5 (live) or Mock generator."""
        if self.data_mode == "live":
            from data.mt5_provider import fetch_closed_candles
            raw = fetch_closed_candles(symbol=self.mt5_symbol, timeframe=timeframe, count=n_candles)
            
            # Приводим метку времени к единому формату UTC epoch seconds.
            # Resolution-independent (pandas 3.x stores datetimes at µs, so the
            # legacy `astype("int64") // 10**9` would return milliseconds).
            if "timestamp_utc" not in raw.columns:
                from data.ingestion import to_epoch_seconds
                raw["timestamp_utc"] = to_epoch_seconds(raw["timestamp"])
                
            df = tag_dataframe(raw, self.cfg["sessions"])
            return df
        else:
            from data.ingestion import fetch_mock_candles
            return fetch_mock_candles(timeframe, n_candles, self.cfg["sessions"])

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = build_all_indicators(df, self.cfg)
        df = add_order_flow_features(df)
        df = candle_anatomy(df)
        df = detect_structure(df, lookback=self.cfg["features"]["structure_lookback"])
        df = add_regime_indicators(df, self.cfg)
        try:
            from features.bifurcation import add_bifurcation_features
            df = add_bifurcation_features(df)
        except Exception as e:
            import logging
            logging.getLogger("realtime.pipeline").warning("bifurcation features skipped: %s", e)

        htf_frames = {}
        ref_tfs = self.cfg.get("features", {}).get("mtf_reference_timeframes", ["M15", "H1"])
        
        for htf in ref_tfs:
            try:
                raw_htf = self._fetch_data_frame(timeframe=htf, n_candles=100)
                if not raw_htf.empty:
                    htf_df = build_all_indicators(raw_htf, self.cfg)
                    htf_frames[htf] = htf_df
            except Exception as e:
                # A failed higher-timeframe fetch silently zeroed mtf_confluence_score
                # for every signal with no trace. Keep the graceful degradation but
                # make it observable so a broken HTF feed can be diagnosed.
                logger.warning(
                    "[%s] higher-timeframe (%s) confluence fetch failed; "
                    "continuing without it: %s",
                    self.asset_key, htf, e,
                )

        if htf_frames:
            df = compute_confluence_score(df, htf_frames, self.cfg)
        else:
            logger.warning(
                "[%s] no higher-timeframe frames available; "
                "mtf_confluence_score defaulted to 0.0", self.asset_key,
            )
            df["mtf_confluence_score"] = 0.0

        df["regime"] = classify_regime_series(df, self.cfg)
        return df

    def generate_signal(self, n_candles: int = 300) -> dict:
        df = self._fetch_data_frame(timeframe=self.timeframe, n_candles=n_candles)

        featured_df = self._build_features(df)
        latest = featured_df.iloc[-1]
        regime = latest["regime"]
        session = str(latest["session"])

        feature_dict = {}
        if self._predictor is not None:
            # Phase 3: feature_cols may include regime_<label> one-hot columns that
            # the live row does not carry (it has only the raw causal `regime` column).
            # Pass the raw row and let ModelPredictor re-synthesize the regime_*
            # columns from `regime`; a genuinely incomplete row (NaN warm-up or a
            # missing non-regime feature) raises, which we map to the warm-up
            # no-trade response exactly as the previous explicit NaN check did.
            try:
                proba = self._predictor.predict_single(latest)
            except (KeyError, ValueError):
                return self._no_trade_response(latest, regime, "Insufficient feature data (warm-up period)")
            ml_p_long, ml_p_short = proba["p_long"], proba["p_short"]
            feature_dict = {
                k: float(v)
                for k, v in latest.items()
                if k in self._predictor.feature_cols and not pd.isna(v)
            }
        else:
            ml_p_long, ml_p_short = 0.5, 0.5

        signal = compute_ensemble_signal(
            regime,
            ml_p_long,
            ml_p_short,
            self.effective_cfg,
            session=session,
            timestamp_utc=int(latest["timestamp_utc"]),
            asset_key=self.asset_key,
        )

        # BOOK GATE (live-only overlay, fail-open): when a BookFeed is attached
        # and the asset has DOM data, the just-closed bar's book features
        # (top-of-book imbalance etc.) either veto a direction the book
        # strongly opposes or add a small confidence boost when it agrees.
        # No feed / no DOM / broken book => signal is passed through unchanged
        # and the payload states the feed was unavailable.
        book_gate = {"decision": "unavailable", "reason": "book feed not attached"}
        book_features = None
        if self.book_feed is not None and signal.bias in ("long", "short"):
            feats = self.book_feed.bar_features(
                self.asset_key, int(latest["timestamp_utc"])
            )
            if feats is not None:
                book_features = feats
                book_gate = self._apply_book_gate(signal, feats)

        entry_price = float(latest["close"])
        atr_val = float(latest["atr"]) if not pd.isna(latest["atr"]) else 1.0

        # Equal-step grid spec (config `signal_grid`): step = step_points or
        # 1.0*ATR (clamped), TP1/2/3 = entry ± 1/2/3*step, SL = entry ∓ 3*step.
        # Per-regime exit policy (signal_grid.regime_overrides) is resolved at
        # signal time so the LIVE targets match the backtest engine.
        reg_name = regime.value if isinstance(regime, RegimeLabel) else str(regime)
        grid_cfg = get_signal_grid(self.cfg, self.asset_cfg, regime=reg_name)
        step = resolve_signal_step(atr_val, grid_cfg)
        tp1_mult = float(grid_cfg.get("tp1_mult", 1.0))
        tp2_mult = float(grid_cfg.get("tp2_mult", 2.0))
        tp3_mult = float(grid_cfg.get("tp3_mult", 3.0))
        stop_mult = float(grid_cfg.get("stop_mult", 3.0))

        if signal.bias == "long":
            entry_zone = [round(entry_price - step * 0.1, 2), round(entry_price + step * 0.1, 2)]
            invalidation = round(entry_price - step * stop_mult, 2)
            targets = [
                round(entry_price + step * tp1_mult, 2),
                round(entry_price + step * tp2_mult, 2),
                round(entry_price + step * tp3_mult, 2),
            ]
        elif signal.bias == "short":
            entry_zone = [round(entry_price - step * 0.1, 2), round(entry_price + step * 0.1, 2)]
            invalidation = round(entry_price + step * stop_mult, 2)
            targets = [
                round(entry_price - step * tp1_mult, 2),
                round(entry_price - step * tp2_mult, 2),
                round(entry_price - step * tp3_mult, 2),
            ]
        else:
            entry_zone, invalidation, targets = None, None, None

        signal_ts = int(latest["timestamp_utc"])
        signal_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{self.strategy_identity['strategy_version']}:{self.asset_key}:{signal_ts}",
        ))
        feature_hash = hashlib.sha256(
            json.dumps(feature_dict, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() if feature_dict else None
        scaleout = grid_cfg.get("scaleout") or {}
        ratios = [float(scaleout.get("tp1_ratio", 1 / 3)), float(scaleout.get("tp2_ratio", 1 / 3))]
        ratios.append(max(0.0, 1.0 - sum(ratios)))
        return {
            "signal_id": signal_id,
            "signal_state": "confirmed" if signal.bias != "no_trade" else "no_trade",
            "strategy_version": self.strategy_identity["strategy_version"],
            "strategy_spec_hash": self.strategy_identity["strategy_spec_hash"],
            "config_hash": self.strategy_identity["config_hash"],
            "model_hash": self.strategy_identity["model_hash"],
            "feature_snapshot_hash": feature_hash,
            "setup_timeframe": self.timeframe,
            "context_timeframes": self.cfg.get("features", {}).get("mtf_reference_timeframes", []),
            "expires_at_utc": signal_ts + 4 * {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400}.get(self.timeframe, 900),
            "target_legs": ([{"price": p, "close_ratio": ratios[i], "label": f"TP{i+1}"} for i, p in enumerate(targets)] if targets else []),
            "confirmation_predicates": ["candle_closed", "regime_allowed", "session_allowed", "ensemble_gate_passed"],
            "confirmed_by": ("systematic:ensemble" if signal.bias != "no_trade" else None),
            "confirmation_time_utc": (signal_ts if signal.bias != "no_trade" else None),
            "bias": signal.bias,
            "confidence": signal.confidence,
            "entry_zone": entry_zone,
            "invalidation": invalidation,
            "targets": targets,
            "step": float(step),
            "reasoning_summary": signal.reasoning_summary,
            "regime": regime.value if isinstance(regime, RegimeLabel) else str(regime),
            "timestamp_utc": signal_ts,
            "session": session,
            "features": feature_dict,
            "book_gate": book_gate,
            "book_features": book_features,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _apply_book_gate(self, signal, feats: dict) -> dict:
        """Live book overlay: veto or confidence boost (mutates ``signal``).

        Fail-open by contract: callers only reach this method when the feed
        returned features, and an unexpected state falls back to 'boost-less
        pass-through' (the signal is never blocked on book plumbing errors).
        """
        bg = self.cfg.get("book_gate") or {}
        if not bg.get("enabled", True):
            return {"decision": "disabled"}
        veto_imb = float(bg.get("veto_imbalance", 0.35))
        boost = float(bg.get("boost_confidence", 0.05))
        imb = float(feats.get("imb5_last", 0.0))
        direction = signal.bias
        opposed = (direction == "long" and imb > veto_imb) or (
            direction == "short" and imb < -veto_imb
        )
        if opposed:
            old_confidence = signal.confidence
            signal.bias = "no_trade"
            signal.confidence = 0.0
            signal.reasoning_summary = (
                f"{signal.reasoning_summary} | BOOK VETO: {direction} blocked, "
                f"imb5_last={imb:+.3f} (threshold {veto_imb:+.3f})"
            )
            return {
                "decision": "veto",
                "bias_blocked": direction,
                "imbalance": imb,
                "threshold": veto_imb,
                "confidence_before": old_confidence,
            }
        if direction in ("long", "short"):
            old_confidence = signal.confidence
            signal.confidence = min(old_confidence + boost, 0.95)
            return {
                "decision": "boost",
                "imbalance": imb,
                "confidence_before": old_confidence,
                "confidence_after": signal.confidence,
            }
        return {"decision": "no_data"}

    def _no_trade_response(self, latest, regime, reason: str) -> dict:
        signal_ts = int(latest["timestamp_utc"])
        signal_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{self.strategy_identity['strategy_version']}:{self.asset_key}:{signal_ts}",
        ))
        return {
            "signal_id": signal_id,
            "signal_state": "no_trade",
            "strategy_version": self.strategy_identity["strategy_version"],
            "strategy_spec_hash": self.strategy_identity["strategy_spec_hash"],
            "config_hash": self.strategy_identity["config_hash"],
            "model_hash": self.strategy_identity["model_hash"],
            "feature_snapshot_hash": None,
            "setup_timeframe": self.timeframe,
            "context_timeframes": self.cfg.get("features", {}).get("mtf_reference_timeframes", []),
            "expires_at_utc": signal_ts,
            "target_legs": [],
            "confirmation_predicates": [],
            "bias": "no_trade",
            "confidence": 0.0,
            "entry_zone": None,
            "invalidation": None,
            "targets": None,
            "reasoning_summary": reason,
            "regime": regime.value if isinstance(regime, RegimeLabel) else str(regime),
            "timestamp_utc": signal_ts,
            "session": str(latest["session"]),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }