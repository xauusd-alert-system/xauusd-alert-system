"""publish_book_signals - T-16 end-to-end producer (model -> bridge -> EA).

Closes the loop the MQL5 book describes in 7.9 and the TZ asks for in T-16:
Python owns data + inference, MQL5 owns execution. This script is the
Python-side producer that turns trained book-protocol models into signal
intents in the shared SQLite bridge:

    candles (sqlite)                         trained ensemble
          |                                        |
          v                                        v
   build_book_features -> apply_normalization -> forward -> model_probability
          |                                                      |
          v                                                      v
   features_hash (sha256)                            ensemble_vote (T-25)
          |                                                      |
          +------------------> SignalIntent <--------------------+
                                        |
                                        v
                       SignalBridgeWriter.write_signal (status='new')

Guards (fail-closed, same philosophy as the rest of the bridge):
* normalization params must come from training (``--models-dir``); a column
  mismatch raises instead of silently mis-scaling features;
* a signal is only written when the ensemble clears TradeLevel AND enough
  member models agree (min_agreement) - otherwise "flat" is logged, not sent;
* ``intent_id`` is derived from (asset, timeframe, last bar, features hash),
  so re-running on the same bar is idempotent (no duplicate intents);
* the intent carries ATR-based SL/TP prices (geometry only - the EA still
  owns execution and its own risk checks);
* probability stored on the intent is the probability of the SIGNALLED
  direction (p for long, 1-p for short), not raw P(up).

Usage (models trained by scripts/run_book_experiments.py)::

    python -m scripts.publish_book_signals \
        --models-dir output/book_experiments_real \
        --db data/market_data_external.sqlite \
        --asset XAUUSD --timeframe M15

On the CURRENT public data the ensemble is expected to stay flat most of the
time (test directional accuracy ~= 50% on real data) - publishing a signal
requires clearing the vote thresholds, which the models rarely do. That is
the intended behaviour: no edge, no trade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from execution.signal_bridge import SignalBridgeWriter, SignalIntent  # noqa: E402
from model.book_nn import BookNetwork  # noqa: E402
from model.book_nn.ensemble_vote import ensemble_vote, model_probability  # noqa: E402
from model.sample_generator import (  # noqa: E402
    DEFAULT_CFG,
    apply_normalization,
    build_book_features,
    load_normalization_params,
)
from scripts.run_book_experiments import load_candles  # noqa: E402

logger = logging.getLogger("publish_book_signals")

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                     "H1": 3600, "H4": 14400, "D1": 86400}


def compute_features_hash(feature_window: np.ndarray, columns: list[str]) -> str:
    """Stable fingerprint of the exact (normalized) inputs the models saw.

    Rounded to 1e-9 so re-runs over identical bars hash identically even
    across BLAS builds; column names are part of the payload so a reordered
    feature frame can never masquerade as the same input.
    """
    payload = {"columns": list(columns),
               "values": np.round(np.asarray(feature_window, dtype=float),
                                  9).tolist()}
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def build_live_window(df: pd.DataFrame, cfg: dict, norm_params,
                      window: int) -> tuple[np.ndarray, pd.DataFrame]:
    """Feature matrix (1, window, D) for the most recent `window` bars."""
    features = build_book_features(df, cfg)
    normed = apply_normalization(features, norm_params)
    normed = normed.replace([np.inf, -np.inf], np.nan)
    if normed.tail(window).isna().any().any():
        raise ValueError("non-finite normalized features in the live window "
                         "(indicator warm-up too short?)")
    frame = normed.tail(window)
    return frame.to_numpy(dtype=float)[None, ...], frame


def load_ensemble(models_dir: str) -> dict[str, BookNetwork]:
    """All trained book models in a directory (``book_<name>.json/.npz``)."""
    nets = {}
    for cfg_path in sorted(Path(models_dir).glob("book_*.json")):
        base = str(cfg_path.with_suffix(""))
        if not Path(base + ".npz").exists():
            continue
        nets[cfg_path.stem[len("book_"):]] = BookNetwork.load(base)
    if not nets:
        raise FileNotFoundError(f"no book_<name>.json/.npz pairs in {models_dir}")
    return nets


def _true_range_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Causal ATR of the last bar (same definition as sample_generator)."""
    high, low, close = (df[c].astype(float) for c in ("high", "low", "close"))
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()],
                   axis=1).max(axis=1)
    return float(tr.rolling(period, min_periods=1).mean().iloc[-1])


def publish(models_dir: str, db: str, bridge: str, asset: str, timeframe: str,
            window: int = DEFAULT_CFG["window"],
            horizon: int = DEFAULT_CFG["horizon"],
            trade_level: float = 0.6, min_agreement: float = 0.6,
            warmup_bars: int = 300, atr_mult: float = 1.5,
            risk_reward: float = 2.0, ttl_seconds: int = 3 * 3600,
            dry_run: bool = False) -> dict:
    """Run one inference pass and (maybe) write one signal intent."""
    norm_path = os.path.join(models_dir, "normalization_params.json")
    norm_params = load_normalization_params(norm_path)
    nets = load_ensemble(models_dir)

    # enough history for indicator warm-up + the live window
    needed = int(warmup_bars) + int(window)
    df = load_candles(asset, timeframe, db)
    if df is None or len(df) < needed:
        raise ValueError(f"need >= {needed} {asset} {timeframe} bars in {db}, "
                         f"got {0 if df is None else len(df)}")
    df = df.tail(needed).reset_index(drop=True)

    cfg = {**DEFAULT_CFG, "window": window, "horizon": horizon,
           "extended": False}
    X, frame = build_live_window(df, cfg, norm_params, window)
    if X.shape[2] != len(norm_params.columns):
        raise ValueError(f"feature width {X.shape[2]} != normalization "
                         f"columns {len(norm_params.columns)} - model/data "
                         "mismatch, refusing to publish")

    probs = {name: model_probability(net.forward(X))
             for name, net in nets.items()}
    vote = ensemble_vote(probs, trade_level=trade_level,
                         min_agreement=min_agreement)["samples"][-1]

    last = df.iloc[-1]
    last_time = int(pd.Timestamp(last["time"]).timestamp())
    fhash = compute_features_hash(X[0], list(frame.columns))
    intent_id = f"{asset}-{timeframe}-{last_time}-{fhash[7:19]}"

    result = {
        "asset": asset, "timeframe": timeframe,
        "last_bar_time_utc": int(last_time),
        "models": sorted(probs),
        "mean_probability_p_up": round(vote["mean_probability"], 6),
        "model_votes": vote["model_votes"],
        "agreement": round(vote["agreement"], 6),
        "signal": vote["signal"],
        "features_hash": fhash,
        "intent_id": intent_id,
        "written": False,
    }

    if vote["signal"] == "flat":
        logger.info("ensemble flat (p_up=%.3f, agreement=%.2f) - nothing sent",
                    vote["mean_probability"], vote["agreement"])
        return result

    direction = 1 if vote["signal"] == "long" else -1
    p_up = vote["mean_probability"]
    entry = float(last["close"])
    atr = _true_range_atr(df)
    stop = atr * atr_mult
    intent = SignalIntent(
        intent_id=intent_id,
        asset=asset,
        direction=direction,
        probability=float(p_up if direction > 0 else 1.0 - p_up),
        entry_price=entry,
        sl_price=round(entry - direction * stop, 2),
        tp_price=round(entry + direction * stop * risk_reward, 2),
        horizon_bars=int(horizon),
        expires_at_utc=last_time + (int(horizon) + 2)
        * TIMEFRAME_SECONDS.get(timeframe, 900),
        features_hash=fhash,
        comment=(f"books-ensemble {'/'.join(sorted(probs))} "
                 f"p_up={p_up:.3f} agree={vote['agreement']:.2f} "
                 f"atr={atr:.2f}"),
    )
    result.update({"direction": direction, "entry_price": entry,
                   "sl_price": intent.sl_price, "tp_price": intent.tp_price})
    if dry_run:
        logger.info("dry-run: would write intent %s", intent.intent_id)
        return result

    with SignalBridgeWriter(bridge, ttl_seconds=ttl_seconds) as writer:
        writer.write_signal(intent)
    result["written"] = True
    logger.info("wrote intent %s (%s, p=%.3f)", intent.intent_id,
                vote["signal"], intent.probability)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models-dir", default="output/book_experiments_real")
    ap.add_argument("--db", default="data/market_data_external.sqlite")
    ap.add_argument("--bridge", default=None,
                    help="bridge sqlite (default: data/ml_signal_bridge.sqlite)")
    ap.add_argument("--asset", default="XAUUSD")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--window", type=int, default=DEFAULT_CFG["window"])
    ap.add_argument("--horizon", type=int, default=DEFAULT_CFG["horizon"])
    ap.add_argument("--trade-level", type=float, default=0.6)
    ap.add_argument("--min-agreement", type=float, default=0.6)
    ap.add_argument("--warmup-bars", type=int, default=300)
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--risk-reward", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    from execution.signal_bridge import default_bridge_path
    bridge = args.bridge or default_bridge_path()
    result = publish(args.models_dir, args.db, bridge, args.asset,
                     args.timeframe, window=args.window, horizon=args.horizon,
                     trade_level=args.trade_level,
                     min_agreement=args.min_agreement,
                     warmup_bars=args.warmup_bars, atr_mult=args.atr_mult,
                     risk_reward=args.risk_reward, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
