"""Diagnostic: confirm the FX 0%-win-rate backtest cause.

Hypothesis: EnsembleBacktester applies a GLOBAL absolute-price slippage
(slippage_points=5 -> 0.05 PRICE units) to every asset, with no per-asset
override.  For low-priced FX (EURUSD ~1.08, GBPUSD ~1.27) 0.05 is ~400-500
pips, which shifts every entry far outside the traded range; the ATR-sized
take-profit targets then sit unreachable and every position exits at its stop
(or a beleaguered timeout) -> a loss.  Gold (~2300) and BTC (~50k) are
untouched by 0.05, which explains why only XAG/EUR/GBP degenerate.

This script reproduces the REAL walk-forward strategy pipeline on a tail slice
(train a model on an earlier portion, score the later portion, run the
EnsembleBacktester) and prints the slippage/spread magnitudes vs market scale,
plus a trade-level sample (entry vs candle open, exit_reason, pnl).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from config.loader import load_config
from scripts.run_backtest import load_asset_history, build_full_df, merge_asset_cfg  # noqa: E402
from model.trainer import build_training_matrix, train_model, calibrate_model, save_model  # noqa: E402
from model.predictor import ModelPredictor  # noqa: E402
from model.ensemble_backtest import EnsembleBacktester  # noqa: E402


def main():
    asset_key = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
    n_tail = int(sys.argv[2]) if len(sys.argv) > 2 else 60000
    cfg = load_config()
    raw = load_asset_history("data/market_data_mt5.sqlite", "M5", asset_key)
    df = build_full_df(cfg, raw, db_path="data/market_data_mt5.sqlite", asset_key=asset_key)
    df = df.tail(n_tail).reset_index(drop=True)

    # Faithful reproduction of run_backtest.strategy_fn_factory
    cfg_inner = merge_asset_cfg(cfg, asset_key, "labeling")
    cfg_inner = merge_asset_cfg(cfg_inner, asset_key, "ensemble")
    X_train, y_train, cols = build_training_matrix(df, cfg=cfg_inner)
    split = int(len(X_train) * 0.7)
    X_fit = X_train.iloc[:split]
    y_fit = y_train.iloc[:split]
    df_eval = df.iloc[len(df) - len(X_train) + split:].copy().reset_index(drop=True)

    import tempfile
    base = train_model(X_fit, y_fit, cfg_inner)
    calib = calibrate_model(base, X_fit, y_fit, cfg_inner)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="diag_model_", suffix=".joblib")
    os.close(tmp_fd)
    try:
        save_model(calib, cols, tmp_path)
        # Phase 3: df_eval carries the raw causal `regime` column, not the
        # regime_<label> one-hots that cols may include (model.use_regime_feature).
        # ModelPredictor re-synthesizes them from `regime` at inference time, so pass
        # the whole raw frame; fillna keeps warm-up NaN rows non-crashing as before.
        preds = ModelPredictor(tmp_path).predict_proba(df_eval.fillna(0.0))
        df_eval["ml_p_long"] = preds["p_long"].values
        df_eval["ml_p_short"] = preds["p_short"].values
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    bt = EnsembleBacktester(cfg_inner, asset_key=asset_key)
    print(f"asset={asset_key} eval_rows={len(df_eval)}")
    print(f"  slippage (price units) = {bt.slippage}")
    print(f"  spread   (price units) = {bt.spread}")
    print(f"  volume={bt.volume} point_value_lot={bt.point_value_lot} commission={bt.commission_per_trade}")

    px = df_eval["close"].mean() if len(df_eval) else float("nan")
    atr = df_eval["atr"].mean() if "atr" in df_eval.columns and len(df_eval) else float("nan")
    print(f"  mean_close={px:.6f}  mean_atr={atr:.6f}")
    if atr == atr and atr > 0:
        print(f"  slippage as % of price = {bt.slippage / px * 100:.3f}%")
        print(f"  slippage / mean_atr = {bt.slippage / atr:.2f}x  (entry shifted <slippage> ATRs off-market)")
        print(f"  spread   / mean_atr = {bt.spread / atr:.2f}x")

    trades = bt.run(df_eval)
    print(f"  n_trades_slice = {len(trades)}")
    if not trades:
        print("  (no trades in this slice)")
        return

    from collections import Counter
    wins = sum(1 for t in trades if t.pnl > 0)
    print(f"  win_rate = {wins / len(trades) * 100:.2f}%  (win/loss = {wins}/{len(trades) - wins})")
    print(f"  exit_reasons = {dict(Counter(t.exit_reason for t in trades))}")
    sample = trades[:8]
    print("\n  sample trades:")
    for t in sample:
        print(
            f"    dir={'long ' if t.direction == 1 else 'short'} "
            f"entry={t.entry_price:.6f} exit={t.exit_price:.6f} "
            f"reason={t.exit_reason} pnl={t.pnl:.4f} tp1={t.tp1_price:.6f} stop={t.stop_price:.6f}"
        )


if __name__ == "__main__":
    main()
