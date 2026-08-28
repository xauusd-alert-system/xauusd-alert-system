"""
Train one per-asset model from the MT5-backed SQLite database.

Example:
    python -m scripts.train_mt5 --symbol XAUUSD
        --db-path data/market_data_mt5.sqlite
        --output output/models/xauusd_direction_model.joblib
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import effective_asset_config, get_signal_grid, load_config
from data.storage import read_candles
from features.candle_anatomy import candle_anatomy
from features.indicators import build_all_indicators
from features.mtf_confluence import compute_confluence_score
from features.order_flow import add_order_flow_features
from features.structure import detect_structure
from labeling.label_generator import generate_labels_from_config, resolve_label_event
from model.trainer import (
    build_training_matrix,
    calibrate_model,
    purged_time_ordered_split,
    save_model,
    train_model,
)
from model.uniqueness import aligned_uniqueness_weights
from regime.classifier import add_regime_indicators, classify_regime_series


def build_full_df(
    df: pd.DataFrame,
    cfg: dict,
    db_path: str = None,
    asset_key: str = None,
    timeframe: str = "M15",
) -> pd.DataFrame:
    """
    Builds features, indicators, MTF confluence, regime labels, and target labels for training.

    Resolve per-asset overrides here as well as in ``main`` because deploy_guard,
    layer validation and diagnostics call this function directly.
    """
    if asset_key is not None:
        cfg = effective_asset_config(cfg, asset_key)
    df = df.copy()
    df = build_all_indicators(df, cfg)
    # Задача 3.1: optional fractional-differentiated close (FFd, Lopez de Prado
    # ch.5). Config-gated: when features.fractional_diff.enabled is false or
    # absent (the default), NOTHING is added and the frame is byte-identical
    # to the baseline pipeline. When true, a `close_fd` column (d/thres from
    # config) is appended BEFORE any downstream feature consumer, so the
    # research feature-selection (Задача 3.3) can admit it via
    # model.feature_subset without touching FEATURE_COLUMNS.
    fd_cfg = cfg.get("features", {}).get("fractional_diff", {}) or {}
    if fd_cfg.get("enabled", False):
        from features.fractional_diff import frac_diff

        fd_series = frac_diff(
            df["close"],
            d=float(fd_cfg.get("d", 0.4)),
            thresh=float(fd_cfg.get("thres", 1e-5)),
        )
        df["close_fd"] = fd_series
    df = add_order_flow_features(df)
    df = candle_anatomy(df)
    df = detect_structure(df, lookback=cfg["features"]["structure_lookback"])
    df = add_regime_indicators(df, cfg)
    # Agent-based bifurcation (features/bifurcation.py) — causal entropy of
    # trend/counter-trend/noise populations; must run after regime/order_flow
    # so it can use adx/cvd/bb_width_percentile.
    try:
        from features.bifurcation import add_bifurcation_features

        df = add_bifurcation_features(df)
    except Exception as e:
        import logging

        logging.getLogger("train_mt5").warning("bifurcation features skipped: %s", e)

    # Загружаем старшие таймфреймы (H1, H4) из SQLite для расчета MTF Confluence
    if db_path and asset_key:
        htf_frames = {}
        ref_tfs = cfg.get("features", {}).get("mtf_reference_timeframes", ["H1", "H4"])
        for htf in ref_tfs:
            try:
                end_ts = int(df["timestamp_utc"].max()) if "timestamp_utc" in df else None
                raw_htf = read_candles(db_path, htf, asset_key, end_ts=end_ts)
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
    df["label"] = generate_labels_from_config(df, cfg, asset_key=asset_key)
    return df


def truncate_raw_before(df: pd.DataFrame, end_date: str, asset_key: str) -> pd.DataFrame:
    """Return raw candles strictly before a UTC cutoff, before feature building."""
    cutoff = pd.Timestamp(end_date)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    cutoff_ts = int(cutoff.timestamp())
    if "timestamp_utc" not in df.columns:
        raise ValueError("raw training frame has no timestamp_utc column")
    out = df.loc[pd.to_numeric(df["timestamp_utc"], errors="coerce") < cutoff_ts].copy()
    if out.empty:
        raise SystemExit(f"No {asset_key} candles strictly before {cutoff.isoformat()}")
    return out.reset_index(drop=True)


def _config_hash(cfg: dict) -> str:
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _purged_oos_calibration(model, X_test: pd.DataFrame, y_test: pd.Series, asset_key: str) -> dict:
    """Mandatory untouched production-split probability report."""
    import numpy as np

    from model.calibration import calibration_report

    if X_test.empty:
        return {"scope": "purged_production_holdout", "n_samples": 0, "available": False}
    probabilities = np.asarray(model.predict_proba(X_test), dtype=float)
    classes = [int(c) for c in getattr(model, "classes_", range(probabilities.shape[1]))]
    y_arr = np.asarray(y_test, dtype=int)
    if len(classes) == 2 and 1 in classes:
        # A configured three-class window with no no_trade rows is normalized
        # from {0:short, 2:long} to binary {0,1} by train_model.
        if set(np.unique(y_arr)) <= {0, 2} and 2 in set(np.unique(y_arr)):
            y_arr = (y_arr == 2).astype(int)
        report = calibration_report(y_arr, probabilities[:, classes.index(1)], asset_name=asset_key)
        report.update({"scope": "purged_production_holdout", "available": True, "class_encoding": classes})
        return report

    # Multiclass confidence calibration: Brier over the full probability vector
    # and ECE of max-confidence versus correctness.
    class_to_col = {c: i for i, c in enumerate(classes)}
    valid = np.asarray([v in class_to_col for v in y_arr])
    if not valid.any():
        return {"scope": "purged_production_holdout", "n_samples": 0, "available": False, "class_encoding": classes}
    p = probabilities[valid]
    yt = y_arr[valid]
    one_hot = np.zeros_like(p)
    for i, value in enumerate(yt):
        one_hot[i, class_to_col[int(value)]] = 1.0
    confidence = p.max(axis=1)
    predicted = np.asarray(classes)[p.argmax(axis=1)]
    correct = (predicted == yt).astype(int)
    report = calibration_report(correct, confidence, asset_name=asset_key)
    report["brier_score_multiclass"] = float(np.mean(np.sum((p - one_hot) ** 2, axis=1)))
    report.update(
        {
            "scope": "purged_production_holdout",
            "available": True,
            "class_encoding": classes,
            "metric_semantics": "confidence_calibration",
        }
    )
    return report


def build_artifact_metadata(
    cfg: dict,
    asset_key: str,
    timeframe: str,
    df: pd.DataFrame,
    y: pd.Series,
    train_rows: int,
    test_rows: int,
    weights_mode: str,
    calibration_report_oos: dict | None = None,
    data_cutoff_utc: str | None = None,
) -> dict:
    """Create the immutable data/target contract stored beside a trained model."""
    timestamps = df.get("timestamp_utc")
    period_start = int(timestamps.min()) if timestamps is not None and len(timestamps) else None
    period_end = int(timestamps.max()) if timestamps is not None and len(timestamps) else None
    counts = {str(k): int(v) for k, v in y.value_counts(dropna=False).to_dict().items()}
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    spread = asset_cfg.get("spread_usd", cfg.get("backtest", {}).get("spread_points", 25) / 100.0)
    slippage = asset_cfg.get("slippage_usd", cfg.get("backtest", {}).get("slippage_points", 5) / 100.0)
    from config.strategy_contract import strategy_identity

    identity = strategy_identity(cfg)
    return {
        "bundle_schema_version": 2,
        **identity,
        "trained_at_utc": datetime.now(UTC).isoformat(),
        "asset_key": asset_key,
        "timeframe": timeframe,
        "data_period": {
            "start_timestamp_utc": period_start,
            "end_timestamp_utc": period_end,
            "cutoff_exclusive_utc": data_cutoff_utc,
        },
        "rows": {"featured": int(len(df)), "labeled": int(len(y)), "train": int(train_rows), "test": int(test_rows)},
        "label_event": resolve_label_event(cfg),
        "labeling": cfg.get("labeling", {}),
        "signal_grid": get_signal_grid(cfg, asset_cfg),
        "execution_cost_assumptions": {
            "spread_price_units": float(spread),
            "slippage_per_side_price_units": float(slippage),
            "source": "static_config",
        },
        "class_counts": counts,
        "effective_config_sha256": _config_hash(cfg),
        "sample_weight_mode": weights_mode,
        "calibration_report_oos": calibration_report_oos or {"scope": "purged_production_holdout", "available": False},
        "calibration_policy": {
            "method": cfg.get("model", {}).get("calibration_method"),
            "split": "purged_time_ordered",
            "sample_weight_mode": (
                weights_mode
                if cfg.get("model", {}).get("calibration_weight_mode", "same_as_training") == "same_as_training"
                else "unweighted"
            ),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Train one MT5-backed model for one symbol.")
    parser.add_argument("--symbol", required=True, help="Internal asset key, e.g. XAUUSD")
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1", "H4"])
    parser.add_argument("--output", required=True, help="Destination .joblib path")
    parser.add_argument(
        "--end-date",
        default=None,
        help=(
            "Drop raw candles at or after this UTC date/time before features are built. "
            "Required for pre-lock A/B model artifacts."
        ),
    )
    parser.add_argument(
        "--label-event",
        choices=["configured", "barrier", "traded"],
        default="configured",
        help=(
            "Target contract. 'configured' uses assets.<SYMBOL>.labeling.event; "
            "explicit barrier/traded is intended for legacy-vs-traded A/B runs."
        ),
    )
    args = parser.parse_args()

    # Resolve every per-asset override before feature generation, labeling,
    # splitting and metadata creation. This is the production parity boundary.
    cfg = effective_asset_config(load_config(), args.symbol)
    if args.label_event != "configured":
        cfg.setdefault("labeling", {})["event"] = args.label_event

    raw = read_candles(args.db_path, args.timeframe, args.symbol)
    if raw.empty:
        raise SystemExit(f"No candles found for {args.symbol} in {args.db_path} timeframe={args.timeframe}")

    # Wave 0 provenance gate: when validation.require_provenance_manifest is
    # true, the frozen manifest must exist and match the raw content BEFORE any
    # feature/label work. Mixing brokers or incomplete history stops the run.
    from data.provenance import provenance_gate

    provenance_gate(cfg, args.db_path, args.timeframe, args.symbol)

    if args.end_date:
        raw = truncate_raw_before(raw, args.end_date, args.symbol)

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
    # Purge the boundary. The labels of the last `horizon` training rows are
    # decided by bars that fall inside the test window, and rolling features
    # (obv, atr_percentile, bb_width_percentile) carry state across the split,
    # which `embargo` covers. Only the training side shrinks - the test slice is
    # identical to the unpurged one, so metrics stay comparable across this fix.
    # backtest/walk_forward.py already does this; production training did not.
    horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 0))
    embargo = int(cfg.get("backtest", {}).get("walk_forward", {}).get("embargo_candles", 0))
    X_train, X_test, y_train, y_test = purged_time_ordered_split(X, y, train_ratio, horizon=horizon, embargo=embargo)

    weights_mode = str(cfg.get("model", {}).get("sample_weight_mode", "uniqueness"))
    if weights_mode == "uniqueness":
        sample_weight = aligned_uniqueness_weights(df.index, X_train.index, horizon=max(1, horizon))
    elif weights_mode == "none":
        sample_weight = None
    else:
        raise SystemExit(f"Unsupported model.sample_weight_mode={weights_mode!r}; expected uniqueness or none")

    base = train_model(X_train, y_train, cfg, sample_weight=sample_weight)
    calibration_weight_mode = cfg.get("model", {}).get("calibration_weight_mode", "same_as_training")
    if calibration_weight_mode == "same_as_training":
        calibration_weight = sample_weight
    elif calibration_weight_mode == "none":
        calibration_weight = None
    else:
        raise SystemExit(
            f"Unsupported model.calibration_weight_mode={calibration_weight_mode!r}; expected same_as_training or none"
        )
    calibrated = calibrate_model(base, X_train, y_train, cfg, sample_weight=calibration_weight)
    calibration_report_oos = _purged_oos_calibration(calibrated, X_test, y_test, args.symbol)
    metadata = build_artifact_metadata(
        cfg,
        args.symbol,
        args.timeframe,
        df,
        y,
        len(X_train),
        len(X_test),
        weights_mode,
        calibration_report_oos=calibration_report_oos,
        data_cutoff_utc=args.end_date,
    )
    save_model(calibrated, cols, args.output, metadata=metadata)

    print(f"symbol={args.symbol}")
    print(f"label_event={metadata['label_event']}")
    print(f"effective_config_sha256={metadata['effective_config_sha256']}")
    print(f"sample_weight_mode={weights_mode}")
    print(f"calibration_weight_mode={calibration_weight_mode}")
    print(f"candles_raw={len(raw)}")
    print(f"rows_featured={len(df)}")
    print(f"rows_labeled_binary={len(X)}")
    print(f"train_rows={len(X_train)}")
    print(f"purge_gap_rows={horizon + embargo} (horizon={horizon} embargo={embargo})")
    print(f"test_rows={len(X_test)}")
    print(f"class_counts={y.value_counts().to_dict()}")
    print(f"saved_model={args.output}")


if __name__ == "__main__":
    main()
