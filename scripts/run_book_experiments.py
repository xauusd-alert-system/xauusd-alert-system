"""Book-protocol experiments: FC vs LSTM vs MH Attention (tasks T-04, T-09).

Runs the book's control-point pipeline on XAUUSD (or any configured asset):

    candles (SQLite or synthetic) -> sample generator (T-02: RSI+MACD+
    geometry, train-only normalization, 60/20/20) -> train each requested
    architecture with Adam + MSE -> per-epoch Train/Val curves (T-17) ->
    test-set MSE + book acceptance stats -> models serialized to files
    (T-20) + curves CSV + summary JSON.

The book's reference points: FC control point = 60 neurons, Swish, Adam,
Linear output (p. 245-246); MH Attention = 8 heads, window_out=8 (p. 512);
attention beat LSTM/CNN on MSE convergence (p. 513). This script makes that
comparison reproducible on OUR data instead of trusting the book's EURUSD
numbers.

Usage:
    python -m scripts.run_book_experiments --models fc,lstm,mha
    python -m scripts.run_book_experiments --models fc --epochs 300 \
        --asset XAUUSD --timeframe M15
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model.book_nn import (  # noqa: E402
    BookNetwork,
    book_fc_baseline_description,
    book_lstm_description,
    book_mha_description,
    fit,
)
from model.sample_generator import (  # noqa: E402
    DEFAULT_CFG,
    generate_book_samples,
    load_normalization_params,
    synthetic_ohlcv,
)

logger = logging.getLogger("run_book_experiments")

MODEL_BUILDERS = {
    "fc": lambda w, d: book_fc_baseline_description(hidden=60, output_dim=2),
    "lstm": lambda w, d: book_lstm_description(hidden=32, output_dim=2),
    "mha": lambda w, d: book_mha_description(heads=8, window_out=8, hidden=60,
                                             output_dim=2, model_dim=32),
}


def load_candles(asset: str, timeframe: str, db_path: str) -> pd.DataFrame | None:
    """Load OHLCV candles from the project SQLite store (time ascending)."""
    if not os.path.isfile(db_path):
        return None
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in ("candles", "bars"):
            if t in tables:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
                need = {"symbol", "timeframe", "time", "open", "high", "low",
                        "close"}
                if need.issubset(set(cols)):
                    df = pd.read_sql_query(
                        f"SELECT time, open, high, low, close, volume FROM {t} "
                        f"WHERE symbol = ? AND timeframe = ? ORDER BY time ASC",
                        con, params=(asset, timeframe))
                    if len(df):
                        df["time"] = pd.to_datetime(df["time"], unit="s",
                                                     utc=True).dt.tz_localize(None)
                        return df
        return None
    finally:
        con.close()


def run_experiment(model_name: str, samples, epochs: int, lr: float,
                   batch_size: int, out_dir: str, window: int, input_dim: int,
                   seed: int) -> dict:
    desc = MODEL_BUILDERS[model_name](window, input_dim)
    net = BookNetwork(desc, window, input_dim, seed=seed)
    started = time.time()
    hist = fit(net, samples.X_train, samples.y_train,
               X_val=samples.X_valid, y_val=samples.y_valid,
               epochs=epochs, batch_size=batch_size, lr=lr, loss="mse",
               seed=seed)
    elapsed = time.time() - started
    from model.book_nn.losses import mse
    test_loss = mse(net.forward(samples.X_test), samples.y_test)[0]
    base = os.path.join(out_dir, f"book_{model_name}")
    files = net.save(base)

    rows = hist.to_rows()
    curves_path = os.path.join(out_dir, f"book_{model_name}_curves.csv")
    pd.DataFrame(rows).to_csv(curves_path, index=False)
    summary = {
        "model": model_name,
        "params": net.num_parameters(),
        "layers": [d["type"] for d in desc],
        "epochs": epochs,
        "seconds": round(elapsed, 2),
        "train_mse_final": rows[-1]["train_loss"],
        "val_mse_final": rows[-1]["val_loss"],
        "test_mse": test_loss,
        "best_epoch": hist.best_epoch,
        "divergence_alerts": hist.alerts,
        "model_files": files,
        "curves_csv": curves_path,
    }
    logger.info("[%s] test MSE %.6f (train %.6f, val %.6f, %d params, %.1fs)",
                model_name, test_loss, summary["train_mse_final"],
                summary["val_mse_final"], summary["params"], elapsed)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="fc,lstm,mha",
                    help="comma-separated: fc,lstm,mha (book architectures)")
    ap.add_argument("--asset", default="XAUUSD")
    ap.add_argument("--timeframe", default="M5",
                    help="candle timeframe for the sample generator")
    ap.add_argument("--db", default="data/market_data_mt5.sqlite")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--window", type=int, default=DEFAULT_CFG["window"])
    ap.add_argument("--horizon", type=int, default=DEFAULT_CFG["horizon"])
    ap.add_argument("--extended-features", action="store_true",
                    help="T-19: add ATR / session vol / volume features")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-dir", default="output/book_experiments")
    ap.add_argument("--synthetic", action="store_true",
                    help="force synthetic candles (no terminal history needed)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.out_dir, exist_ok=True)

    df = None if args.synthetic else load_candles(args.asset, args.timeframe, args.db)
    data_source = "sqlite"
    if df is None:
        df = synthetic_ohlcv(n=20000, seed=args.seed)
        data_source = "synthetic"
        logger.warning("no %s %s candles in %s - using SYNTHETIC data (results "
                       "are a pipeline smoke test, not market evidence)",
                       args.asset, args.timeframe, args.db)

    cfg = {"window": args.window, "horizon": args.horizon,
           "extended": bool(args.extended_features),
           "target_mode": "multi_horizon"}  # book-style 2-neuron output
    norm_path = os.path.join(args.out_dir, "normalization_params.json")
    samples = generate_book_samples(df, cfg, norm_params_path=norm_path)
    params = load_normalization_params(norm_path)
    logger.info("samples %s from %s data; normalization params -> %s",
                samples.split_sizes(), data_source, norm_path)

    input_dim = len(samples.feature_columns)
    results = []
    for model_name in [m.strip() for m in args.models.split(",") if m.strip()]:
        if model_name not in MODEL_BUILDERS:
            ap.error(f"unknown model {model_name!r}; known: {sorted(MODEL_BUILDERS)}")
        results.append(run_experiment(model_name, samples, args.epochs, args.lr,
                                      args.batch_size, args.out_dir,
                                      args.window, input_dim, args.seed))

    summary = {
        "asset": args.asset, "timeframe": args.timeframe,
        "data_source": data_source, "bars": len(df),
        "window": args.window, "horizon": args.horizon,
        "extended_features": bool(args.extended_features),
        "feature_columns": samples.feature_columns,
        "split": samples.split_sizes(),
        "normalization": params.to_dict(),
        "results": results,
        "notes": [
            "Book reference (EURUSD, p. 513): MH Attention MSE ~0.41 vs LSTM "
            "~0.435 vs CNN ~0.43 after 1000 epochs.",
            "XAUUSD numbers are NOT transferable from the book - that is why "
            "this experiment exists.",
        ],
    }
    out_path = os.path.join(args.out_dir, "summary.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps({r["model"]: {"test_mse": r["test_mse"],
                                   "val_mse": r["val_mse_final"],
                                   "params": r["params"]} for r in results},
                     indent=2))
    print(f"summary -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
