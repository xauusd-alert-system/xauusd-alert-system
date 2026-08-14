import sys
import os

sys.path.insert(0, os.path.abspath("."))

import numpy as np
import pandas as pd

from config.loader import load_config
from scripts.deflated_sharpe import _apply_variant, _build_fold_frames, _prepare_fold_frame
from scripts.run_backtest import (
    load_asset_history,
    build_full_df,
    truncate_before,
    merge_asset_cfg,
)
from model.ensemble_backtest import EnsembleBacktester
from backtest.metrics import trades_to_dataframe

cfg = load_config()
asset = "BTCUSD"
timeframe = cfg["assets"][asset].get("timeframe", "M5")
db_path = cfg.get("general", {}).get("db_path")

raw = load_asset_history(db_path, timeframe, asset)
raw = truncate_before(raw, "2026-08-08", asset)
df_full = build_full_df(cfg, raw, db_path=db_path, asset_key=asset)

variants = {
    "current": {},
    "tight": {"signal_grid": {"stop_mult": 2.0, "breakeven_trigger_atr": 0.5}},
    "wide": {"signal_grid": {"stop_mult": 4.0, "breakeven_trigger_atr": 1.0, "tp3_mult": 4.0}},
    "progress_stop": {"signal_grid": {"progress_stop_enabled": True, "progress_stop_ratio": 0.5, "progress_stop_atr": 0.3}},
}

windows, frames = _build_fold_frames(df_full, cfg, asset, max_folds=None)

# Собираем матрицу фолдов: строки = варианты, колонки = фолды
fold_matrix = []
for name, overrides in variants.items():
    cfg_v = _apply_variant(cfg, asset, overrides)
    fold_sums = []
    for fold_i, fdf in enumerate(frames):
        fdf_run = _prepare_fold_frame(fdf, name, fold_i, 42)
        cfg_run = merge_asset_cfg(cfg_v, asset, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset)
        trades = engine.run(fdf_run.reset_index(drop=True))
        tdf = trades_to_dataframe(trades)
        fold_sums.append(tdf["pnl"].sum() if len(tdf) else 0.0)
    fold_matrix.append(fold_sums)

M = np.asarray(fold_matrix)
print("Fold matrix shape:", M.shape)
print("Mean per trial:", M.mean(axis=1))
print("Std per trial:", M.std(axis=1))

# Пример одного сплита: первые 7 фолдов IS, следующие 7 OOS
n_obs = M.shape[1]
is_cols = list(range(0, min(7, n_obs)))
oos_cols = list(range(min(7, n_obs), min(14, n_obs)))

sr_is = M[:, is_cols].mean(axis=1) / (M[:, is_cols].std(axis=1) + 1e-12)
sr_oos = M[:, oos_cols].mean(axis=1) / (M[:, oos_cols].std(axis=1) + 1e-12)

print("\nSingle split example:")
print("IS SR:  ", np.round(sr_is, 3))
print("OOS SR: ", np.round(sr_oos, 3))
print("x_var (IS SR) =", np.var(sr_is))
print("Regression slope =", np.cov(sr_is, sr_oos, ddof=1)[0, 1] / np.var(sr_is, ddof=1))