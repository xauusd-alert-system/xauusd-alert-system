"""
Realtime inference pipeline: wires MT5 live data -> features -> regime -> model -> ensemble
into a single callable that produces the structured signal JSON required by the
FastAPI service, MT5 Auto-Trader, and Telegram bot.
"""
import logging
import os
import time
from datetime import datetime, timezone
import pandas as pd

from config.loader import load_config
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from features.structure import detect_structure
from features.mtf_confluence import compute_confluence_score
from regime.classifier import add_regime_indicators, classify_regime_series, RegimeLabel
from model.predictor import ModelPredictor
from model.ensemble import compute_ensemble_signal
from data.session_tagger import tag_dataframe

from copy import deepcopy

logger = logging.getLogger("realtime_pipeline")


class RealtimePipeline:
    def __init__(
        self,
        cfg: dict = None,
        model_path: str = None,
        asset_key: str = None,
        data_mode: str = "live",
    ):
        self.cfg = cfg or load_config()
        self.data_mode = data_mode

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
        self.timeframe = self.cfg.get("market_data", {}).get("timeframe", "M5")
        self._predictor = ModelPredictor(self.model_path) if self.model_path and os.path.exists(self.model_path) else None

        # Эффективный конфиг с asset-specific переопределением ensemble и labeling
        self.effective_cfg = deepcopy(self.cfg)
        asset_ensemble = self.asset_cfg.get("ensemble")
        if asset_ensemble:
            merged_ensemble = deepcopy(self.cfg.get("ensemble", {}))
            merged_ensemble.update(asset_ensemble)
            self.effective_cfg["ensemble"] = merged_ensemble

        asset_labeling = self.asset_cfg.get("labeling")
        if asset_labeling:
            merged_labeling = deepcopy(self.cfg.get("labeling", {}))
            merged_labeling.update(asset_labeling)
            self.effective_cfg["labeling"] = merged_labeling

    def _fetch_data_frame(self, timeframe: str, n_candles: int) -> pd.DataFrame:
        """Fetches and prepares DataFrame directly from MT5 (live) or Mock generator."""
        if self.data_mode == "live":
            from data.mt5_provider import fetch_closed_candles
            raw = fetch_closed_candles(symbol=self.mt5_symbol, timeframe=timeframe, count=n_candles)
            
            # Приводим метку времени к единому формату UTC epoch seconds
            if "timestamp_utc" not in raw.columns:
                raw["timestamp_utc"] = (raw["timestamp"].astype("int64") // 10**9)
                
            df = tag_dataframe(raw, self.cfg["sessions"])
            return df
        else:
            from data.ingestion import fetch_mock_candles
            return fetch_mock_candles(timeframe, n_candles, self.cfg["sessions"])

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = build_all_indicators(df, self.cfg)
        df = candle_anatomy(df)
        df = detect_structure(df, lookback=self.cfg["features"]["structure_lookback"])
        df = add_regime_indicators(df, self.cfg)

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
            asset_key=self.asset_key
        )

        entry_price = float(latest["close"])
        atr_val = float(latest["atr"]) if not pd.isna(latest["atr"]) else 1.0

        lab_cfg = self.asset_cfg.get("labeling") or self.cfg.get("labeling", {})
        tp1_mult = lab_cfg.get("tp1_atr_multiplier", 1.0)
        tp2_mult = lab_cfg.get("tp2_atr_multiplier", 1.8)
        tp3_mult = lab_cfg.get("tp3_atr_multiplier", 2.8)
        stop_mult = lab_cfg.get("stop_atr_multiplier", 1.0)

        if signal.bias == "long":
            entry_zone = [round(entry_price - atr_val * 0.1, 2), round(entry_price + atr_val * 0.1, 2)]
            invalidation = round(entry_price - atr_val * stop_mult, 2)
            targets = [
                round(entry_price + atr_val * tp1_mult, 2),
                round(entry_price + atr_val * tp2_mult, 2),
                round(entry_price + atr_val * tp3_mult, 2),
            ]
        elif signal.bias == "short":
            entry_zone = [round(entry_price - atr_val * 0.1, 2), round(entry_price + atr_val * 0.1, 2)]
            invalidation = round(entry_price + atr_val * stop_mult, 2)
            targets = [
                round(entry_price - atr_val * tp1_mult, 2),
                round(entry_price - atr_val * tp2_mult, 2),
                round(entry_price - atr_val * tp3_mult, 2),
            ]
        else:
            entry_zone, invalidation, targets = None, None, None

        return {
            "bias": signal.bias,
            "confidence": signal.confidence,
            "entry_zone": entry_zone,
            "invalidation": invalidation,
            "targets": targets,
            "reasoning_summary": signal.reasoning_summary,
            "regime": regime.value if isinstance(regime, RegimeLabel) else str(regime),
            "timestamp_utc": int(latest["timestamp_utc"]),
            "session": session,
            "features": feature_dict,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _no_trade_response(self, latest, regime, reason: str) -> dict:
        return {
            "bias": "no_trade",
            "confidence": 0.0,
            "entry_zone": None,
            "invalidation": None,
            "targets": None,
            "reasoning_summary": reason,
            "regime": regime.value if isinstance(regime, RegimeLabel) else str(regime),
            "timestamp_utc": int(latest["timestamp_utc"]),
            "session": str(latest["session"]),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }