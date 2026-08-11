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
from features.order_flow import add_order_flow_features
from regime.classifier import add_regime_indicators, classify_regime_series
from labeling.label_generator import generate_labels_from_config
from data.storage import read_candles
from model.trainer import (
    build_training_matrix,
    train_model,
    calibrate_model,
    save_model,
    DegenerateLabelSpaceError,
)
from model.predictor import ModelPredictor
from model.ensemble_backtest import EnsembleBacktester
from backtest.walk_forward import run_walk_forward, generate_windows


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
    """Возвращает cfg с объединённым указанным section (ensemble/labeling/model) из asset_cfg."""
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
    df = add_order_flow_features(df)
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


def _maybe_downgrade_three_class(cfg_inner: dict, train_df: pd.DataFrame,
                                asset_key: str) -> dict:
    """Use a binary model for this fold when three-class no-trade labels are absent.

    The configured three-class semantics remain intact for production and for folds
    that have enough no-trade examples.  A fold with virtually none cannot support
    a calibrated no-trade probability, however; making it explicitly binary avoids
    both repeated {0,2} remap warnings and identity-only calibration.
    """
    model_cfg = cfg_inner.get("model", {})
    if not model_cfg.get("include_zero_class", False) or "label" not in train_df:
        return cfg_inner

    asset_model = cfg_inner.get("assets", {}).get(asset_key, {}).get("model", {})
    minimum = float(asset_model.get("min_no_trade_frac", model_cfg.get("min_no_trade_frac", 0.01)))
    labels = train_df["label"].dropna()
    no_trade_fraction = float((labels == 0).mean()) if len(labels) else 0.0
    if no_trade_fraction >= minimum:
        return cfg_inner

    downgraded = copy.deepcopy(cfg_inner)
    downgraded.setdefault("model", {})["include_zero_class"] = False
    # This marker is deliberately fold-local: it documents why a configured
    # three-class model was fit as binary without changing the asset's policy.
    downgraded["model"]["include_zero_class_effectively_binary"] = True
    print(
        "[run_backtest] INFO: %s fold has %.3f%% no_trade labels (< %.3f%%); "
        "using calibrated binary short/long model for this fold."
        % (asset_key, no_trade_fraction * 100, minimum * 100)
    )
    return downgraded


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
        # Per-asset model flags (assets.<key>.model: use_regime_feature /
        # include_zero_class) must reach build_training_matrix, otherwise the
        # GBPUSD v4 backtest silently trains the GLOBAL binary model instead of
        # the per-asset 3-class + regime-feature model the live trader uses.
        cfg_inner = merge_asset_cfg(cfg_inner, asset_key, "model")
        cfg_inner = _maybe_downgrade_three_class(cfg_inner, train_df, asset_key)

        X_train, y_train, cols = build_training_matrix(train_df, cfg=cfg_inner)
        test_df_eval = test_df.copy()
        # Neutral 0.5/0.5 is the baseline for every row: it can never pass a
        # filter, so any fold we fail to model contributes no trades instead of
        # a bogus signal.
        test_df_eval["ml_p_long"] = 0.5
        test_df_eval["ml_p_short"] = 0.5
        calibrated = None

        if len(X_train) >= 30 and y_train.nunique() >= 2:
            # W3 (audit 2026-08-10): weight training rows by average label
            # uniqueness so overlapping horizon-labels don't over-represent the
            # information that is actually unique (Lopez de Prado, AFML ch.4).
            # run_walk_forward has already purged the tail rows whose labels
            # reach into the test window; uniqueness weights handle the residual
            # overlap among the surviving rows. Weights are keyed by the train
            # frame's positional index and aligned to the rows build_training_matrix
            # actually keeps (it drops NaN-label/feature rows).
            from model.uniqueness import average_uniqueness_weights
            horizon = int(cfg_inner.get("labeling", {}).get("horizon_candles_n", 36))
            try:
                uniq = average_uniqueness_weights(len(train_df), horizon)
                w_series = pd.Series(uniq, index=train_df.index)
                sw = w_series.reindex(X_train.index).fillna(1.0).to_numpy()
            except Exception:
                sw = None
            try:
                base = train_model(X_train, y_train, cfg_inner, sample_weight=sw)
                calibrated = calibrate_model(base, X_train, y_train, cfg_inner)
            except DegenerateLabelSpaceError as exc:
                # Data condition, not a defect: this window cannot produce a model
                # whose probabilities decode into an honest p_long/p_short (see
                # model.trainer._normalize_label_space). Previously the raw XGBoost
                # "Invalid classes inferred from unique values of `y`" ValueError
                # escaped here and aborted the WHOLE multi-fold/multi-asset run.
                # Degrade just this fold to "no signal" and keep going, loudly.
                print(f"[run_backtest] WARNING: fold degraded to no-signal -- {exc}")
                calibrated = None

        if calibrated is not None:
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
                # W13 (audit 2026-08-10): warm-up rows carry NaN features. The
                # previous fillna(0.0) scored them as VALID 0.0-feature rows (0.0
                # is meaningful for z-score features), so the backtest opened
                # extra trades at the start of each test window that the live
                # trader (which returns no_trade on incomplete features) never
                # sees. To match live behaviour, rows with any NaN in the model's
                # feature columns are set to the NEUTRAL 0.5/0.5 (never a signal),
                # and only complete rows are predicted.
                feat_cols = [c for c in cols if c in test_df_eval.columns]
                complete = test_df_eval[feat_cols].notna().all(axis=1) if feat_cols \
                    else pd.Series(True, index=test_df_eval.index)
                test_df_eval["ml_p_long"] = 0.5
                test_df_eval["ml_p_short"] = 0.5
                if complete.any():
                    # Phase 3: pass the full frame; ModelPredictor re-synthesizes
                    # regime_* one-hot columns from the raw `regime` column and
                    # selects only its saved feature_cols.
                    preds = predictor.predict_proba(test_df_eval[complete])
                    test_df_eval.loc[complete, "ml_p_long"] = preds["p_long"].values
                    test_df_eval.loc[complete, "ml_p_short"] = preds["p_short"].values
            except Exception:
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
    parser.add_argument("--no-journal", action="store_true",
                        help="Do not append this run to logs/trial_journal.csv")
    parser.add_argument("--allow-locked", action="store_true",
                        help="Allow test windows overlapping the locked hold-out")
    args = parser.parse_args()

    cfg = load_config()
    assets = cfg.get("assets", {})
    if args.asset not in assets:
        raise SystemExit(f"Unknown asset: {args.asset}")

    asset_cfg = assets[args.asset]
    model_path = asset_cfg["model_path"]
    # Per-asset timeframe override (assets.<key>.timeframe) wins over --timeframe.
    timeframe = asset_cfg.get("timeframe") or args.timeframe

    raw = load_asset_history(args.db_path, timeframe, args.asset)
    df = build_full_df(cfg, raw, db_path=args.db_path, asset_key=args.asset)

    print(f"Loaded {len(df)} rows for {args.asset} from {args.db_path}")
    print(f"Running Ensemble ML Walk-Forward Backtest...")

    from scripts.trial_journal import enforce_locked_holdout
    windows_probe = generate_windows(
        df, cfg["backtest"]["walk_forward"]["train_window_days"],
        cfg["backtest"]["walk_forward"]["test_window_days"],
        cfg["backtest"]["walk_forward"]["step_days"])
    enforce_locked_holdout(cfg, windows_probe, "run_backtest", allow=args.allow_locked)

    results = run_walk_forward(df, cfg, strategy_fn_factory(cfg, model_path, asset_key=args.asset))
    if not results:
        raise SystemExit("No walk-forward folds produced.")

    results_df = pd.DataFrame([r for r in results])
    os.makedirs("logs", exist_ok=True)
    results_df.to_csv(f"logs/backtest_{args.asset.lower()}.csv", index=False)
    print(f"Saved metrics to logs/backtest_{args.asset.lower()}.csv")

    # Quant audit 0.1: PF-median vs positive-fold arithmetic consistency.
    # Positive folds MUST be counted over VALID (non-empty) folds; a median
    # PF > 1 with < 50% positive VALID folds means the two statistics refer
    # to different fold sets.
    from backtest.metrics import summarize_folds, fold_sign_test
    summary = summarize_folds(results)
    print(f"\nFold summary: {summary['positive_folds_valid']}/{summary['valid_folds']} "
          f"positive valid folds ({summary['positive_folds_pct_valid']}%) | "
          f"median PF (valid) = {summary['median_pf_valid']} | "
          f"empty folds = {summary['n_folds'] - summary['valid_folds']}")
    st = fold_sign_test(summary["positive_folds_valid"], summary["valid_folds"])
    print(f"Sign test vs 50%: z={st['z']}, p(one-sided)={st['p_one_sided']}")
    if summary["inconsistent"]:
        print(f"WARNING: {summary['note']} -- re-check the aggregate tables "
              "(positive folds must use valid folds only).")
    pd.DataFrame([summary]).to_csv(f"logs/backtest_{args.asset.lower()}_fold_summary.csv", index=False)

    # Append-only trial journal (audit: N_trials for DSR comes from the real
    # project history, not from the last grid).
    if not args.no_journal:
        from scripts.trial_journal import log_trial
        log_trial(
            experiment="run_backtest",
            asset=args.asset,
            params={"timeframe": timeframe, "db_path": args.db_path},
            metrics={"n_folds": summary["n_folds"], "valid_folds": summary["valid_folds"],
                     "positive_folds_valid": summary["positive_folds_valid"],
                     "median_pf_valid": summary["median_pf_valid"],
                     "total_pnl": float(results_df["total_pnl"].sum())
                     if "total_pnl" in results_df.columns else None})


if __name__ == "__main__":
    main()