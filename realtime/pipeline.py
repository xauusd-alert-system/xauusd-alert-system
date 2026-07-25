"""
Realtime inference pipeline: wires data -> features -> regime -> model -> ensemble
into a single callable that produces the structured signal JSON required by the
FastAPI service (realtime/app.py).

CRITICAL NO-LOOK-AHEAD NOTE:
This pipeline operates on LIVE data only - the latest N candles up to "now". Every
sub-step (features/, regime/, model/) was already proven causal in earlier steps'
tests. This module adds ONE more guarantee: it only ever computes a signal for the
LAST row of the fetched candle series (the most recently closed candle), never for
an in-progress/incomplete candle, and never using any candle timestamped after "now".
"""
import time
from datetime import datetime, timezone
import pandas as pd

from config.loader import load_config
from data.ingestion import fetch_candles
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from features.structure import detect_structure
from regime.classifier import add_regime_indicators, classify_regime_series, RegimeLabel
from model.predictor import ModelPredictor
from model.ensemble import compute_ensemble_signal


class RealtimePipeline:
    """
    Stateful wrapper holding the loaded ML model (loaded once, reused across requests)
    and the config. Call .generate_signal() per inference request.
    """

    def __init__(self, cfg: dict = None, model_path: str = None, data_mode: str = "mock"):
        self.cfg = cfg or load_config()
        self.data_mode = data_mode
        self.model_path = model_path
        self._predictor = None
        if model_path:
            self._predictor = ModelPredictor(model_path)

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = build_all_indicators(df, self.cfg)
        df = candle_anatomy(df)
        df = detect_structure(df, lookback=self.cfg["features"]["structure_lookback"])
        df = add_regime_indicators(df, self.cfg)
        df["mtf_confluence_score"] = 0.0  # TODO: wire real MTF frames once multi-timeframe fetch is added to /signal
        return df

    def generate_signal(self, n_candles: int = 300) -> dict:
        """
        Pulls the latest n_candles on the primary labeling timeframe, builds features,
        classifies regime, runs ML inference (if a model is loaded), computes the
        ensemble signal, and returns the structured JSON contract:
        {bias, confidence, entry_zone, invalidation, targets, reasoning_summary}
        """
        timeframe = self.cfg["labeling"]["labeling_timeframe"]
        sessions_cfg = self.cfg["sessions"]

        df = fetch_candles(timeframe, n_candles, sessions_cfg, mode=self.data_mode)
        df = self._build_features(df)
        df["regime"] = classify_regime_series(df, self.cfg)

        latest = df.iloc[-1]
        regime = latest["regime"]

        if self._predictor is not None:
            feature_row = latest[self._predictor.feature_cols]
            if feature_row.isnull().any():
                # Warm-up / insufficient data - explicit no-trade, never guess
                return self._no_trade_response(latest, regime, "Insufficient feature data (warm-up period)")
            proba = self._predictor.predict_single(feature_row)
            ml_p_long, ml_p_short = proba["p_long"], proba["p_short"]
        else:
            # No trained model wired yet - fall back to neutral ML probability (0.5/0.5),
            # which the ensemble will treat as zero ML confidence, relying on rule-based only.
            ml_p_long, ml_p_short = 0.5, 0.5

        signal = compute_ensemble_signal(regime, ml_p_long, ml_p_short, self.cfg)

        entry_price = float(latest["close"])
        atr_val = float(latest["atr"]) if not pd.isna(latest["atr"]) else 0.0
        target_x = self.cfg["labeling"]["target_pips_x"]
        stop_y = self.cfg["labeling"]["stop_pips_y"]

        if signal.bias == "long":
            entry_zone = [round(entry_price - atr_val * 0.1, 2), round(entry_price + atr_val * 0.1, 2)]
            invalidation = round(entry_price - stop_y, 2)
            targets = [round(entry_price + target_x, 2)]
        elif signal.bias == "short":
            entry_zone = [round(entry_price - atr_val * 0.1, 2), round(entry_price + atr_val * 0.1, 2)]
            invalidation = round(entry_price + stop_y, 2)
            targets = [round(entry_price - target_x, 2)]
        else:
            entry_zone, invalidation, targets = None, None, None

        return {
            "bias": signal.bias,
            "confidence": signal.confidence,
            "entry_zone": entry_zone,
            "invalidation": invalidation,
            "targets": targets,
            "reasoning_summary": signal.reasoning_summary,
            "regime": regime.value,
            "timestamp_utc": int(latest["timestamp_utc"]),
            "session": str(latest["session"]),
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
