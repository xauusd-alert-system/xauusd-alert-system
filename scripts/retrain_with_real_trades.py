"""
Weekly Retraining Script using Historical Candle Data + Real Executed Trades.
Extracts real trades from executed_trades SQLite table, joins features & outcomes,
and retrains per-asset ML models.

Usage:
    python -m scripts.retrain_with_real_trades
"""
import os
import sys
import json
import logging
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.storage import read_candles
from data.trade_logger import read_executed_trades
from scripts.train_mt5 import build_full_df
from model.trainer import (
    FEATURE_COLUMNS,
    build_training_matrix,
    time_ordered_split,
    train_model,
    calibrate_model,
    save_model,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("weekly_retrainer")


def prepare_real_trades_df(trades_df: pd.DataFrame, feature_cols: list) -> tuple[pd.DataFrame, pd.Series]:
    """
    Parses JSON features and converts real trade outcome into target label y.
    y = 1 if outcome was upper/long favorable, 0 if lower/short favorable.
    """
    if trades_df.empty:
        return pd.DataFrame(columns=feature_cols), pd.Series(dtype=int)

    rows_x = []
    rows_y = []

    for _, row in trades_df.iterrows():
        try:
            feats = json.loads(row["features"]) if isinstance(row["features"], str) else (row["features"] or {})
            if not feats:
                continue

            # Ensure all required feature columns exist
            feat_row = {col: feats.get(col, 0.0) for col in feature_cols}
            bias = str(row["bias"]).lower()
            outcome = int(row["outcome"])  # 1 = profitable/breakeven, 0 = loss

            # Label mapping:
            # Long trade & win -> 1; Long trade & loss -> 0
            # Short trade & win -> 0; Short trade & loss -> 1
            if bias == "long":
                target = 1 if outcome == 1 else 0
            elif bias == "short":
                target = 0 if outcome == 1 else 1
            else:
                continue

            rows_x.append(feat_row)
            rows_y.append(target)
        except Exception as e:
            logger.warning(f"Error parsing trade row {row.get('ticket')}: {e}")

    if not rows_x:
        return pd.DataFrame(columns=feature_cols), pd.Series(dtype=int)

    X_real = pd.DataFrame(rows_x)[feature_cols]
    y_real = pd.Series(rows_y, dtype=int)
    return X_real, y_real


def retrain_asset(asset_key: str, cfg: dict):
    asset_cfg = cfg["assets"][asset_key]
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    timeframe = cfg.get("market_data", {}).get("timeframe", "M5")
    model_path = asset_cfg["model_path"]

    logger.info(f"--- Weekly Retraining for {asset_key} ({asset_cfg['mt5_symbol']}) ---")

    # 1. Historical Candles Data
    raw_candles = read_candles(db_path, timeframe, asset_key)
    if raw_candles.empty:
        logger.warning(f"No historical candles found for {asset_key}. Skipping.")
        return

    full_df = build_full_df(
        raw_candles,
        cfg,
        db_path=db_path,
        asset_key=asset_key,
        timeframe=timeframe,
    )
    X_hist, y_hist, available_cols = build_training_matrix(full_df)

    # 2. Real Trades Data
    trades_df = read_executed_trades(db_path, symbol=asset_key)
    X_real, y_real = prepare_real_trades_df(trades_df, available_cols)

    logger.info(f"[{asset_key}] Historical samples: {len(X_hist)}, Real trade samples: {len(X_real)}")

    if not X_real.empty:
        # Combine historical data with real trades
        X_combined = pd.concat([X_hist, X_real], ignore_index=True)
        y_combined = pd.concat([y_hist, y_real], ignore_index=True)
    else:
        X_combined, y_combined = X_hist, y_hist

    if len(X_combined) < 500:
        logger.warning(f"Not enough training samples for {asset_key}: {len(X_combined)}")
        return

    train_ratio = cfg["model"].get("train_ratio", 0.8)
    X_train, X_test, y_train, y_test = time_ordered_split(X_combined, y_combined, train_ratio)

    base_model = train_model(X_train, y_train, cfg)
    calibrated_model = calibrate_model(base_model, X_train, y_train, cfg)
    save_model(calibrated_model, available_cols, model_path)

    logger.info(f"✅ Successfully retrained & saved model for {asset_key} -> {model_path}")


def main():
    cfg = load_config()
    assets = cfg.get("assets", {})
    enabled_assets = [k for k, v in assets.items() if v.get("enabled", False)]

    logger.info(f"🚀 Starting Weekly ML Retraining for enabled assets: {enabled_assets}")

    for asset_key in enabled_assets:
        try:
            retrain_asset(asset_key, cfg)
        except Exception as e:
            logger.error(f"Failed retraining for {asset_key}: {e}", exc_info=True)

    logger.info("🎉 Weekly ML Retraining completed for all assets!")


if __name__ == "__main__":
    main()
