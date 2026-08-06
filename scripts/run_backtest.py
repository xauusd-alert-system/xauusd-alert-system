import argparse
import os
import sqlite3
import sys
import copy

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from features.structure import detect_structure
from features.mtf_confluence import compute_confluence_score
from regime.classifier import add_regime_indicators, classify_regime_series
from labeling.label_generator import generate_labels_from_config
from data.storage import read_candles
from model.trainer import (
    build_training_matrix,
    train_model,
    calibrate_model,
    save_model,
)
from model.predictor import ModelPredictor
from model.ensemble_backtest import EnsembleBacktester
from backtest.walk_forward import run_walk_forward


def load_asset_history(db_path: str, timeframe: str, asset_key: str) -> pd.DataFrame:
    table = f"ohlcv_{timeframe.lower()}"
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            f"""
            SELECT
                timestamp_utc,
                open,
                high,
                low,
                close,
                volume,
                session
            FROM {table}
            WHERE symbol = ?
            ORDER BY timestamp_utc
            """,
            conn,
            params=(asset_key,),
        )
    if df.empty:
        raise ValueError(f"No rows found for {asset_key} in {table}")
    df["timestamp_utc"] = df["timestamp_utc"].astype("int64")
    df["timestamp"] = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
    return df


def merge_asset_cfg(cfg: dict, asset_key: str, section: str) -> dict:
    """Возвращает cfg с объединённым указанным section (ensemble/labeling) из asset_cfg."""
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    base_section = cfg.get(section, {})
    asset_section = asset_cfg.get(section)
    if asset_section:
        merged = copy.deepcopy(base_section)
        merged.update(asset_section)
    else:
        merged = copy.deepcopy(base_section)
    cfg_copy = copy.deepcopy(cfg)
    cfg_copy[section] = merged
    return cfg_copy


def build_full_df(cfg: dict, raw_df: pd.DataFrame, db_path: str, asset_key: str) -> pd.DataFrame:
    cfg = merge_asset_cfg(cfg, asset_key, "labeling")
    df = raw_df.copy()
    df = build_all_indicators(df, cfg)
    df = candle_anatomy(df)
    df = detect_structure(df, lookback=cfg["features"]["structure_lookback"])
    df = add_regime_indicators(df, cfg)

    htf_frames = {}
    ref_tfs = cfg.get("features", {}).get("mtf_reference_timeframes", ["M15", "H1"])
    for htf in ref_tfs:
        try:
            raw_htf = read_candles(db_path, htf, asset_key)
            if not raw_htf.empty:
                htf_df = build_all_indicators(raw_htf, cfg)
                htf_frames[htf] = htf_df
        except Exception:
            pass

    if htf_frames:
        df = compute_confluence_score(df, htf_frames, cfg)
    else:
        df["mtf_confluence_score"] = 0.0

    df["regime"] = classify_regime_series(df, cfg)
    df["label"] = generate_labels_from_config(df, cfg)
    return df


def strategy_fn_factory(cfg, model_path: str, asset_key: str):
    # HIGH 11: walk-forward folds must NEVER overwrite the production model file.
    # Saving each fold's model to the prod path destroys the deployed model used by
    # the live trader every time a backtest runs. Models trained per fold are only
    # needed transiently for scoring the out-of-sample window, so we keep them in a
    # temporary, per-fold file and remove it afterwards.
    import tempfile

    def strategy_fn(train_df, test_df, cfg_inner):
        cfg_inner = merge_asset_cfg(cfg_inner, asset_key, "labeling")
        cfg_inner = merge_asset_cfg(cfg_inner, asset_key, "ensemble")

        X_train, y_train, cols = build_training_matrix(train_df, cfg=cfg_inner)
        test_df_eval = test_df.copy()

        if len(X_train) >= 30 and y_train.nunique() >= 2:
            base = train_model(X_train, y_train, cfg_inner)
            calibrated = calibrate_model(base, X_train, y_train, cfg_inner)
            # Save to a temp file only; do not touch the production model.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="wf_model_", suffix=".joblib"
            )
            os.close(tmp_fd)
            try:
                save_model(calibrated, cols, tmp_path)
                predictor = ModelPredictor(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            try:
                # Phase 3: cols may include regime_<label> one-hot columns that the
                # eval frame does not carry (it has the raw causal `regime` column).
                # ModelPredictor re-synthesizes regime_* from `regime` at inference
                # time, so pass the whole raw frame; fillna keeps warm-up NaN rows
                # non-crashing exactly as before, and predict_proba ignores any
                # non-feature columns (it selects only its saved feature_cols).
                preds = predictor.predict_proba(test_df_eval.fillna(0.0))
                test_df_eval["ml_p_long"] = preds["p_long"].values
                test_df_eval["ml_p_short"] = preds["p_short"].values
            except Exception:
                test_df_eval["ml_p_long"] = 0.5
                test_df_eval["ml_p_short"] = 0.5
        else:
            test_df_eval["ml_p_long"] = 0.5
            test_df_eval["ml_p_short"] = 0.5

        from backtest.metrics import trades_to_dataframe, compute_metrics

        engine = EnsembleBacktester(cfg_inner, asset_key=asset_key)
        trades = engine.run(test_df_eval.reset_index(drop=True))
        trades_df = trades_to_dataframe(trades)
        return compute_metrics(trades_df)

    return strategy_fn


def main():
    parser = argparse.ArgumentParser(description="Run walk-forward backtest for one asset from SQLite.")
    parser.add_argument("--asset", required=True, help="Internal asset key, e.g. XAUUSD")
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    args = parser.parse_args()

    cfg = load_config()
    assets = cfg.get("assets", {})
    if args.asset not in assets:
        raise SystemExit(f"Unknown asset: {args.asset}")

    asset_cfg = assets[args.asset]
    model_path = asset_cfg["model_path"]

    raw = load_asset_history(args.db_path, args.timeframe, args.asset)
    df = build_full_df(cfg, raw, db_path=args.db_path, asset_key=args.asset)

    print(f"Loaded {len(df)} rows for {args.asset} from {args.db_path}")
    print(f"Running Ensemble ML Walk-Forward Backtest...")

    results = run_walk_forward(df, cfg, strategy_fn_factory(cfg, model_path, asset_key=args.asset))
    if not results:
        raise SystemExit("No walk-forward folds produced.")

    results_df = pd.DataFrame([r for r in results])
    os.makedirs("logs", exist_ok=True)
    results_df.to_csv(f"logs/backtest_{args.asset.lower()}.csv", index=False)
    print(f"Saved metrics to logs/backtest_{args.asset.lower()}.csv")


if __name__ == "__main__":
    main()