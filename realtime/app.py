"""
FastAPI inference service exposing the /signal endpoint.

Design decision: the RealtimePipeline is instantiated ONCE at app startup (loading
the model from disk a single time) rather than per-request, since model loading is
comparatively expensive and the model is immutable during the service's lifetime.
Config is also loaded once and shared - config.loader already caches it globally.

No secrets are read here directly; MODEL_PATH is the only path-like env var and it
points to a local file, not a credential.
"""
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from config.loader import load_config, get_env
from realtime.pipeline import RealtimePipeline

app = FastAPI(title="XAUUSD Predictive Alert System", version="0.1.0")

CFG = load_config()
MODEL_PATH = get_env("MODEL_PATH", default=None)
DATA_MODE = get_env("DATA_MODE", default="mock")  # "mock" or "live" - controlled via env, never hardcoded

pipeline = RealtimePipeline(cfg=CFG, model_path=MODEL_PATH, data_mode=DATA_MODE)


class SignalResponse(BaseModel):
    bias: str
    confidence: float
    entry_zone: Optional[List[float]] = None
    invalidation: Optional[float] = None
    targets: Optional[List[float]] = None
    reasoning_summary: str
    regime: str
    timestamp_utc: int
    session: str
    generated_at: str


@app.get("/health")
def health():
    """Basic liveness check - does not run the full pipeline."""
    return {"status": "ok", "data_mode": DATA_MODE, "model_loaded": pipeline._predictor is not None}


@app.get("/signal", response_model=SignalResponse)
def get_signal(n_candles: int = 300):
    """
    Runs the full pipeline (data -> features -> regime -> model -> ensemble) and
    returns the structured signal JSON. n_candles controls how much history is
    pulled for feature warm-up (must exceed regime.min_candles_for_regime + buffer).
    """
    try:
        result = pipeline.generate_signal(n_candles=n_candles)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Signal generation failed: {str(e)}")
