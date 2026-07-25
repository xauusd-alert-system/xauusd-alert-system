"""
CLI entry point for walk-forward backtest + model training.
For each walk-forward fold:
  1. Trains an XGBoost model on the train window
  2. Evaluates the ensemble (rule-based + ML) on the test window
  3. Logs per-fold metrics to stdout and saves a summary CSV

Usage:
    python scripts/run_backtest.py                   # uses config.yaml walk_forward settings
    python scripts/run_backtest.py --timeframe M15   # override timeframe
    python scripts/run_backtest.py --mock            # use mock data (no API key needed)

Saves trained model to: models/model_latest.joblib
Saves metrics CSV to:   logs/backtest_results.csv
"""
import os
import sys
import argparse
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.ingestion import fetch_candles
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from regime.classifier import add_regime_indicators, classify_regime_series
from labeling.label_generator import generate_labels_from_config
from model.trainer import build_training_matrix, time_ordered_split, train_model, calibrate_model, save_model
from model.predictor import ModelPredictor
from model.ensemble import compute_ensemble_signal
from backtest.engine import EventDrivenBacktester, rule_based_signal
from backtest.metrics import trades_to_dataframe, compute_metrics
from backtest.walk_forward import generate_windows, run_walk_forward


MODEL_OUT = "models/model_latest.joblib"
METRICS_OUT = "logs/backtest_results.csv"


def build_full_df(cfg, timeframe, mode, n_candles=5000):
    sessions_cfg = cfg["sessions"]
    df = fetch_candles(timeframe, n_candles, sessions_cfg, mode=mode)
    df = build_all_indicators(df, cfg)
    df = candle_anatomy(df)
    df = add_regime_indicators(df, cfg)
    df["mtf_confluence_score"] = 0.0
    df["regime"] = classify_regime_series(df, cfg)
    df["label"] = generate_labels_from_config(df, cfg)
    return df


def strategy_fn(train_df, test_df, cfg):
    """Train on train_df, evaluate ensemble on test_df, return metrics dict."""
    # Train model on train window
    X, y, cols = build_training_matrix(train_df)
    model_path = MODEL_OUT

    if len(X) >= 30 and y.nunique() >= 2:
        base = train_model(X, y, cfg)
        calibrated = calibrate_model(base, X, y, cfg)
        os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
        save_model(calibrated, cols, model_path)
        predictor = ModelPredictor(model_path)
    else:
        predictor = None

    # Run ensemble backtest on test window
    engine = EventDrivenBacktester(cfg)
    trades = engine.run(test_df.reset_index(drop=True))
    trades_df = trades_to_dataframe(trades)
    metrics = compute_metrics(trades_df)
    return metrics


MOCK_CFG_OVERRIDES = {
    "labeling": {"target_pips_x": 3.0, "stop_pips_y": 2.0},
    "backtest": {
        "spread_points": 30, "slippage_points": 10,
        "initial_balance": 500.0, "risk_per_trade_pct": 1.0,
    },
}


def apply_mock_overrides(cfg: dict) -> dict:
    import copy
    cfg = copy.deepcopy(cfg)
    for section, overrides in MOCK_CFG_OVERRIDES.items():
        cfg[section].update(overrides)
    return cfg


def main():
    parser = argparse.ArgumentParser(description="Run walk-forward backtest.")
    parser.add_argument("--timeframe", type=str, default=None)
    parser.add_argument("--mock", action="store_true", help="Use mock data instead of live API")
    parser.add_argument("--candles", type=int, default=5000, help="Number of candles to fetch")
    args = parser.parse_args()

    cfg = load_config()
    mode = "mock" if args.mock else "live"
    if args.mock:
        cfg = apply_mock_overrides(cfg)
    timeframe = args.timeframe or cfg["labeling"]["labeling_timeframe"]

    print(f"Building full dataset: timeframe={timeframe}, mode={mode}, candles={args.candles}")
    df = build_full_df(cfg, timeframe, mode, n_candles=args.candles)
    print(f"Dataset ready: {len(df)} rows, {df['label'].value_counts(dropna=False).to_dict()}")

    print("\nRunning walk-forward backtest...")
    results = run_walk_forward(df, cfg, strategy_fn)

    if not results:
        print("No walk-forward folds generated - dataset may be too short for configured windows.")
        return

    rows = []
    for i, r in enumerate(results):
        w = r.pop("window")
        print(f"  Fold {i+1}: trades={r.get('n_trades',0)}, "
              f"win_rate={r.get('win_rate', 'N/A')}, "
              f"pf={r.get('profit_factor', 'N/A')}, "
              f"pnl={r.get('total_pnl', 'N/A')}")
        rows.append(r)

    results_df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(METRICS_OUT) or ".", exist_ok=True)
    results_df.to_csv(METRICS_OUT, index=False)
    print(f"\nMetrics saved to {METRICS_OUT}")
    print(f"Model saved to {MODEL_OUT}")
    print("\nAggregate across all folds:")
    print(results_df[["n_trades", "win_rate", "profit_factor", "total_pnl"]].describe().to_string())


if __name__ == "__main__":
    main()


