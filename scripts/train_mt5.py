"""
Train one per-asset model from the MT5-backed SQLite database.

Example:
    python -m scripts.train_mt5 --symbol XAUUSD --db-path data/market_data_mt5.sqlite --output output/models/xauusd_direction_model.joblib
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.storage import read_candles
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from features.structure import detect_structure
from features.mtf_confluence import compute_confluence_score
from features.order_flow import add_order_flow_features
from regime.classifier import add_regime_indicators, classify_regime_series
from labeling.label_generator import generate_labels_from_config
from model.trainer import (
    build_training_matrix,
    time_ordered_split,
    train_model,
    calibrate_model,
    save_model,
)


def build_full_df(
    df: pd.DataFrame,
    cfg: dict,
    db_path: str = None,
    asset_key: str = None,
    timeframe: str = "M15",
) -> pd.DataFrame:
    """
    Builds features, indicators, MTF confluence, regime labels, and target labels for training.
    """
    df = df.copy()
    df = build_all_indicators(df, cfg)
    df = add_order_flow_features(df)
    df = candle_anatomy(df)
    df = detect_structure(df, lookback=cfg["features"]["structure_lookback"])
    df = add_regime_indicators(df, cfg)

    # Загружаем старшие таймфреймы (H1, H4) из SQLite для расчета MTF Confluence
    if db_path and asset_key:
        htf_frames = {}
        ref_tfs = cfg.get("features", {}).get("mtf_reference_timeframes", ["H1", "H4"])
        for htf in ref_tfs:
            try:
                raw_htf = read_candles(db_path, htf, asset_key)
                if not raw_htf.empty:
                    htf_df = build_all_indicators(raw_htf, cfg)
                    htf_frames[htf] = htf_df
            except Exception:
                pass  # Если старшего таймфрейма пока нет в базе, пропустим

        if htf_frames:
            df = compute_confluence_score(df, htf_frames, cfg)
        else:
            df["mtf_confluence_score"] = 0.0
    else:
        df["mtf_confluence_score"] = 0.0

    df["regime"] = classify_regime_series(df, cfg)
    df["label"] = generate_labels_from_config(df, cfg)
    return df


def main():
    parser = argparse.ArgumentParser(description="Train one MT5-backed model for one symbol.")
    parser.add_argument("--symbol", required=True, help="Internal asset key, e.g. XAUUSD")
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1", "H4"])
    parser.add_argument("--output", required=True, help="Destination .joblib path")
    args = parser.parse_args()

    cfg = load_config()

    raw = read_candles(args.db_path, args.timeframe, args.symbol)
    if raw.empty:
        raise SystemExit(f"No candles found for {args.symbol} in {args.db_path} timeframe={args.timeframe}")

    # Собираем полный датасет с учетом MTF фичей
    df = build_full_df(
        raw,
        cfg,
        db_path=args.db_path,
        asset_key=args.symbol,
        timeframe=args.timeframe,
    )

    X, y, cols = build_training_matrix(df, cfg=cfg)
    if len(X) < 500:
        raise SystemExit(f"Not enough labeled rows after feature/label prep for {args.symbol}: {len(X)}")
    if y.nunique() < 2:
        raise SystemExit(f"Need both classes present for {args.symbol}; got only one class.")

    train_ratio = cfg["model"].get("train_ratio", 0.8)
    X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio)

    base = train_model(X_train, y_train, cfg)
    calibrated = calibrate_model(base, X_train, y_train, cfg)
    save_model(calibrated, cols, args.output)

    print(f"symbol={args.symbol}")
    print(f"candles_raw={len(raw)}")
    print(f"rows_featured={len(df)}")
    print(f"rows_labeled_binary={len(X)}")
    print(f"train_rows={len(X_train)}")
    print(f"test_rows={len(X_test)}")
    print(f"class_counts={y.value_counts().to_dict()}")
    print(f"saved_model={args.output}")


if __name__ == "__main__":
    main()