"""
Weekly Retraining Script using Historical Candle Data + Real Executed Trades.
Extracts real trades from executed_trades SQLite table, joins features & outcomes,
and retrains per-asset ML models.

Usage:
    python -m scripts.retrain_with_real_trades

Step 5d (#26/#27) conservative "documented assumptions" hardening:

#26 - real trades ARE merged into retraining (see retrain_asset, binary mode
      only). The merge is exactly the documented contract:
      `prepare_real_trades_df` parses the JSON features of each executed trade
      and maps its outcome to a binary label:
          long trade & win          -> target 1
          long trade & loss         -> target 0
          short trade & win         -> target 0
          short trade & loss        -> target 1
      Rows whose features dict is missing/unparseable/empty, or whose bias /
      outcome value is unexpected, are DROPPED (logged) - they never crash the
      run and never surface as partial/poisoned rows in X_real/y_real.

#27 - a retraining run must NOT silently "succeed" when the real-trade payload
      is missing. `retrain_asset` always returns per-asset stats, and `main()`
      maps them to an honest exit code consumed by scripts/overnight.py stage 4:
        EXIT_OK (0)              - every asset fully retrained; in binary mode
                                   real trades merged (or legitimately none
                                   exist yet on a fresh account).
        EXIT_PAYLOAD_MISSING (1) - any asset hard-failed (exception, no candles,
                                   too few samples), OR the real-trade merge was
                                   skipped for EVERY enabled asset because of an
                                   incompatible model configuration
                                   (include_zero_class / use_regime_feature).
      In the skip-merge case the model is still trained & saved on fresh history
      (a partial retrain), but the run returns non-zero so the overnight wrapper
      reports stage 4 FAILED / notifies instead of showing a silent green tick.

Exit codes (consumed by scripts/overnight.py stage 4):
    0  - all enabled assets retrained OK.
    1  - missing real-trade payload (see #27 above).
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

# Documented exit codes for main()
# (see module docstring - consumed by scripts/overnight.py).
EXIT_OK = 0
EXIT_PAYLOAD_MISSING = 1


def prepare_real_trades_df(trades_df: pd.DataFrame, feature_cols: list) -> tuple[pd.DataFrame, pd.Series]:
    """
    Parses JSON features and converts real trade outcome into target label y.
    y = 1 if outcome was upper/long favorable, 0 if lower/short favorable.

    Contract (Step 5d #26):
        * Always returns a (X_real, y_real) pair with the same feature_cols
          columns, so the caller can rely on `if not X_real.empty`.
        * Rows whose features JSON is missing/unparseable/empty are dropped
          (logged) - they never crash the run and never surface as partial rows.
        * A row with an unexpected `bias` or `outcome` value is also dropped
          (logged) rather than mapped to an arbitrary label.
    """
    if trades_df.empty:
        return pd.DataFrame(columns=feature_cols), pd.Series(dtype=int)

    rows_x = []
    rows_y = []

    for _, row in trades_df.iterrows():
        try:
            feats = json.loads(row["features"]) if isinstance(row["features"], str) else (row["features"] or {})
            if not feats:
                logger.warning(f"Skipping trade {row.get('ticket')}: empty features dict")
                continue

            # Ensure all required feature columns exist
            feat_row = {col: feats.get(col, 0.0) for col in feature_cols}
            bias = str(row["bias"]).lower()
            raw_outcome = row["outcome"]
            try:
                outcome = int(float(raw_outcome))
            except (TypeError, ValueError):
                logger.warning(f"Skipping trade {row.get('ticket')}: bad outcome {raw_outcome!r}")
                continue

            # Label mapping:
            # Long trade & win -> 1; Long trade & loss -> 0
            # Short trade & win -> 0; Short trade & loss -> 1
            if bias == "long":
                target = 1 if outcome == 1 else 0
            elif bias == "short":
                target = 0 if outcome == 1 else 1
            else:
                logger.warning(f"Skipping trade {row.get('ticket')}: unexpected bias {bias!r}")
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


def retrain_asset(asset_key: str, cfg: dict) -> dict:
    """Retrain one asset, merging real executed trades when compatible.

    Returns a per-asset stats dict (Step 5d #27):
        {
            "asset": asset_key,
            "ok": bool,            # False => process exit code 1
            "samples": int,        # combined X rows (0 for skipped assets)
            "real_trades": int,    # X_real rows merged (0 if merge skipped)
            "reason": str,         # human-readable status
        }
    """
    asset_cfg = cfg["assets"][asset_key]
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    timeframe = asset_cfg.get("timeframe") or cfg.get("market_data", {}).get("timeframe", "M5")
    model_path = asset_cfg["model_path"]

    logger.info(f"--- Weekly Retraining for {asset_key} ({asset_cfg['mt5_symbol']}) ---")

    # 1. Historical Candles Data
    raw_candles = read_candles(db_path, timeframe, asset_key)
    if raw_candles.empty:
        logger.warning(f"No historical candles found for {asset_key}. Skipping.")
        return {"asset": asset_key, "ok": False, "samples": 0, "real_trades": 0,
                "reason": "no_candles"}

    full_df = build_full_df(
        raw_candles,
        cfg,
        db_path=db_path,
        asset_key=asset_key,
        timeframe=timeframe,
    )
    # Per-asset model flags (Stage 4)
    asset_model = cfg.get("assets", {}).get(asset_key, {}).get("model", {})
    model_cfg = {**cfg.get("model", {}), **asset_model}
    three_class = bool(model_cfg.get("include_zero_class", False))
    use_regime_feature = bool(model_cfg.get("use_regime_feature", False))

    # 2. Historical Candles Data (always) + Real Trades Data (binary mode only)
    # NOTE: real executed trades are only ever binary outcomes (win/loss per
    # direction) and cannot be expressed in the 3-class label space
    # {0: short, 1: no_trade, 2: long}. Merging them in 3-class mode would inject
    # a fake "no_trade" class with directional features, so the merge is skipped
    # and the 3-class model is trained purely on labeled historical candles.
    # Likewise, with use_regime_feature=true the logged features JSON does not
    # contain the regime_<label> one-hot columns (they are synthesized from the
    # raw regime column, which is not persisted with executed trades), so the
    # real-trade merge is skipped to avoid feeding incomplete feature rows in.
    if three_class or use_regime_feature:
        if three_class:
            reason = "skip_merge_three_class"
            logger.warning(
                f"[{asset_key}] include_zero_class=true; skipping real-trade merge "
                "(binary outcomes incompatible with 3-class labels)"
            )
        else:
            reason = "skip_merge_regime_feature"
            logger.warning(
                f"[{asset_key}] use_regime_feature=true; skipping real-trade merge "
                "(logged features lack the regime_<label> one-hot columns)"
            )
        X_combined, y_combined, available_cols = build_training_matrix(full_df, cfg=cfg)
        real_trades = 0
    else:
        X_hist, y_hist, available_cols = build_training_matrix(full_df, cfg=cfg)
        trades_df = read_executed_trades(db_path, symbol=asset_key)
        X_real, y_real = prepare_real_trades_df(trades_df, available_cols)
        real_trades = len(X_real)

        logger.info(f"[{asset_key}] Historical samples: {len(X_hist)}, Real trade samples: {len(X_real)}")

        if not X_real.empty:
            X_combined = pd.concat([X_hist, X_real], ignore_index=True)
            y_combined = pd.concat([y_hist, y_real], ignore_index=True)
        else:
            X_combined, y_combined = X_hist, y_hist
        reason = "ok"

    if len(X_combined) < 500:
        logger.warning(f"Not enough training samples for {asset_key}: {len(X_combined)}")
        return {"asset": asset_key, "ok": False, "samples": len(X_combined),
                "real_trades": real_trades, "reason": "too_few_samples"}

    train_ratio = cfg["model"].get("train_ratio", 0.8)
    X_train, X_test, y_train, y_test = time_ordered_split(X_combined, y_combined, train_ratio)

    base_model = train_model(X_train, y_train, cfg)
    calibrated_model = calibrate_model(base_model, X_train, y_train, cfg)
    save_model(calibrated_model, available_cols, model_path)

    logger.info(f"✅ Successfully retrained & saved model for {asset_key} -> {model_path}")

    # Step 5d #27: an asset whose real-trade merge was skipped (reason
    # "skip_merge_*") is reported as NOT fully-ok even though the model was
    # trained & saved on history. Nothing real was folded in, so the run must
    # not present as a fully successful retrain (that would be a silent no-op
    # for #26).
    ok_flag = reason == "ok"
    return {"asset": asset_key, "ok": ok_flag, "samples": len(X_combined),
            "real_trades": real_trades, "reason": reason}


def main() -> int:
    cfg = load_config()
    assets = cfg.get("assets", {})
    enabled_assets = [k for k, v in assets.items() if v.get("enabled", False)]

    logger.info(f"🚀 Starting Weekly ML Retraining for enabled assets: {enabled_assets}")

    stats = []
    for asset_key in enabled_assets:
        try:
            stats.append(retrain_asset(asset_key, cfg))
        except Exception as e:
            logger.error(f"Failed retraining for {asset_key}: {e}", exc_info=True)
            stats.append({"asset": asset_key, "ok": False, "samples": 0,
                          "real_trades": 0, "reason": f"exception: {e}"})

    ok_assets = [s["asset"] for s in stats if s.get("ok")]
    failed_assets = [s["asset"] for s in stats if not s.get("ok")]
    skipped_merge = [
        s["asset"] for s in stats
        if not s.get("ok") and str(s.get("reason", "")).startswith("skip_merge")
    ]
    hard_failed = [a for a in failed_assets if a not in skipped_merge]

    logger.info(f"Attempted {len(enabled_assets)} asset(s); OK: {len(ok_assets)}, "
                f"hard failed: {len(hard_failed)}, merge skipped: {len(skipped_merge)}")
    for s in stats:
        logger.info(f"  [{s['asset']}] ok={s['ok']} samples={s['samples']} "
                    f"real_trades={s['real_trades']} ({s['reason']})")

    # Step 5d #27: surface real problems instead of a silent green exit code.
    if hard_failed:
        logger.warning(
            "Retraining finished with hard-failed assets: %s. Returning non-zero so the "
            "overnight stage-4 wrapper marks this stage FAILED and Telegram is notified "
            "(#27 - a real failure must not look like a success).", hard_failed
        )
        return EXIT_PAYLOAD_MISSING
    if stats and len(skipped_merge) == len(stats):
        logger.warning(
            "Real-trade merge was skipped for ALL enabled assets (%s) because of the model "
            "configuration (include_zero_class / use_regime_feature). Nothing real was folded "
            "into retraining (#26), so the run returns non-zero to surface the missing payload "
            "(#27) instead of a silent green tick.", skipped_merge
        )
        return EXIT_PAYLOAD_MISSING

    logger.info("🎉 Weekly ML Retraining completed for all assets!")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
