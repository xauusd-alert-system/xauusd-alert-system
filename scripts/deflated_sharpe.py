"""
Deflated Sharpe / CSCV assessment for a single asset's config family.

Answers the question the grid-searches never could: after ~700 hyper-parameter
combinations were tried on the same walk-forward data, is the CHOSEN config's
edge real, or the best draw of many? Two complementary answers:

1. **Deflated Sharpe Ratio (DSR)** — `backtest/deflated_sharpe.py`: the
   observed per-trade Sharpe of a config, deflated by the EXPECTED MAXIMUM
   Sharpe under N trials (skew/kurtosis corrected). DSR = probability that
   the true Sharpe > 0 after the selection-bias correction. Also reports
   PSR(0) (no deflation) and the Minimum Track Record Length (MinTRL).

2. **CSCV Probability of Backtest Overfitting (PBO)** — Bailey et al. (2015):
   over all half-block splits of the fold-return matrix, how often does the
   in-sample-best config land in the bottom half OUT-OF-SAMPLE?

Design (honesty requirements, mirroring `scripts/run_backtest.py`):

- Strictly time-ordered walk-forward windows, sliced by the SHARED
  `backtest.walk_forward.split_fold_frames`, so the label-horizon purge and the
  embargo apply here exactly as they do in the reported backtest. A harness that
  skips the purge fits on rows whose labels resolve inside its own test window
  and then calls the result out-of-sample.
- Per-fold models are trained on the train window ONLY and saved to temp files
  (HIGH 11: production models are never touched).
- The SAME per-fold model scores every config variant of an asset, so the
  variant comparison isolates the config (grid/conf/BE) — not model noise.
- A "null" variant (random 0.5±noise probabilities, no model) is always
  included as a negative control: it must come out with DSR ~= 0.5 and a
  large MinTRL, or the machinery is broken.
- A fold VOTES on the gate only if it carries at least
  `MIN_TRADES_FOR_VALID_FOLD` trades, and the fold condition weighs money as
  well as votes (see `fold_health`). Counting a one-trade fold as a verdict,
  or a majority of tiny positive folds as a result, is how a losing family
  passes a checklist.
- `--historical-trials` (default 729 = the full project grid-search history)
  deflates with the TOTAL number of trials ever tried; `dsr_trials` uses only
  the family evaluated in this run (smaller, milder deflation). Correlated
  trials (same folds) make the effective N smaller, so using the full count
  is the conservative direction.
- `--end-date` stops the sample before `validation.locked_holdout` so the gate
  can be computed WITHOUT `--allow-locked`. Burning the lock to compute the
  gate would invalidate condition 7 of the gate itself.

Usage (real data, user machine):

    python -m scripts.deflated_sharpe --asset GBPUSD
    python -m scripts.deflated_sharpe --asset EURUSD --historical-trials 200
    python -m scripts.deflated_sharpe --asset XAUUSD --variants current,wide,null
    python -m scripts.deflated_sharpe --asset XAUUSD --end-date 2026-08-08

Without a DB the script falls back to SYNTHETIC demo data (biased probs by
design, so the machinery demonstrably detects a real edge) — the numbers are
then NOT real and the report says so.
"""

import argparse
import copy
import json
import math
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, get_signal_grid
from data.ingestion import to_epoch_seconds
from scripts.run_backtest import (
    load_asset_history,
    build_full_df,
    merge_asset_cfg,
    truncate_before,
    _maybe_downgrade_three_class,
)
from backtest.walk_forward import generate_windows, split_fold_frames, bar_seconds
from backtest.metrics import trades_to_dataframe, compute_metrics
# NOTE: backtest.deflated_sharpe also exports a `decision_gate`, but this module
# defines its own (the 7-condition admission checklist below). Importing both put
# two different functions under one name, with the local def silently winning.
from backtest.deflated_sharpe import (
    annualized_sharpe,
    probabilistic_sharpe_ratio,
    deflated_sharpe_ratio,
    minimum_track_record_length,
    cscv_pbo,
    effective_number_trials,
    n_eff_participation_ratio,
)
from model.uniqueness import compute_trade_uniqueness, average_uniqueness_weights
from backtest.metrics import block_bootstrap_t
from model.ensemble_backtest import EnsembleBacktester
from model.trainer import (
    build_training_matrix,
    train_model,
    calibrate_model,
    save_model,
    DegenerateLabelSpaceError,
)
from model.predictor import ModelPredictor

# ---------------------------------------------------------------------------
# Gate constants
#
# A fold below MIN_TRADES_FOR_VALID_FOLD says nothing about the config: fold 9
# of the 2026-08-14 XAUUSD run contained a single trade (-122.24) and used to
# cast a full negative vote, while fold 7 lost -2293.40 over 152 trades and
# cast exactly one negative vote as well.
# ---------------------------------------------------------------------------

MIN_TRADES_FOR_VALID_FOLD = 10
POS_FOLD_SHARE_MIN = 0.55
# The report's own verdict ladder calls PBO in (0.20, 0.30] "HIGH overfit risk",
# so admitting at 0.30 meant the gate passed what the report condemned.
PBO_MAX = 0.20

FOLD_CONDITION = "folds: total PnL > 0, PnL ex-best fold > 0, 55% positive"
PBO_CONDITION = f"PBO < {PBO_MAX:.2f}"

# ---------------------------------------------------------------------------
# Variant families (config deltas applied on top of config/config.yaml).
# "current" = the shipped per-asset config; "null" = random-prob negative
# control (never trades on information).
# ---------------------------------------------------------------------------

GENERIC_VARIANTS: dict = {
    "current": {},
    "tight": {  # mean-reversion style: early BE + tighter stop
        "signal_grid": {"stop_mult": 2.0, "breakeven_trigger_atr": 0.5},
    },
    "wide": {  # trend style: no early BE + wider stop + further TP3
        "signal_grid": {"stop_mult": 4.0, "breakeven_trigger_atr": 1.0, "tp3_mult": 4.0},
    },
    "progress_stop": {  # Task 6: early cut if < 0.3x ATR progress within 0.5x horizon
        "signal_grid": {"progress_stop_enabled": True, "progress_stop_ratio": 0.5, "progress_stop_atr": 0.3},
    },
    "null": None,
}

GBP_VARIANTS: dict = {
    "current": {},  # = v4 (post-sync config): stop 3.0, BE 1.0, tp2 2.5, tp3 3.0, conf 0.80, h36
    "v3_early_be": {  # pre-v4 (EUR-style early-BE package)
        "signal_grid": {"stop_mult": 2.0, "breakeven_trigger_atr": 0.5,
                        "tp2_mult": 2.0, "tp3_mult": 3.0},
        "ensemble": {"min_confidence_to_alert": 0.85},
        "labeling": {"horizon_candles_n": 48},
    },
    "v4a": {  # commented candidate in the old config: tp3 4.0 + conf 0.85
        "signal_grid": {"stop_mult": 3.0, "breakeven_trigger_atr": 1.0,
                        "tp2_mult": 2.5, "tp3_mult": 4.0},
        "ensemble": {"min_confidence_to_alert": 0.85},
        "labeling": {"horizon_candles_n": 48},
    },
    "v4b_trailing": {  # trailing-runner candidate (engine code path exists)
        "signal_grid": {"stop_mult": 3.0, "breakeven_trigger_atr": 1.0,
                        "tp2_mult": 2.5, "tp3_mult": 3.0, "trailing_atr_mult": 2.0},
        "ensemble": {"min_confidence_to_alert": 0.80},
        "labeling": {"horizon_candles_n": 36},
    },
    "progress_stop": {  # Task 6: progress-stop candidate
        "signal_grid": {"stop_mult": 3.0, "breakeven_trigger_atr": 1.0,
                        "tp2_mult": 2.5, "tp3_mult": 3.0,
                        "progress_stop_enabled": True, "progress_stop_ratio": 0.5,
                        "progress_stop_atr": 0.3},
        "ensemble": {"min_confidence_to_alert": 0.80},
        "labeling": {"horizon_candles_n": 36},
    },
    "legacy": {  # Phase-0+1 global defaults; regime_overrides: None strips the
        # shipped per-regime policy so the comparison stays honest (patch-value
        # None = key removal, see _apply_variant).
        "signal_grid": {"stop_mult": 3.0, "breakeven_trigger_atr": 1.0,
                        "tp2_mult": 2.0, "tp3_mult": 3.0,
                        "regime_overrides": None},
        "ensemble": {"min_confidence_to_alert": 0.60},
        "labeling": {"horizon_candles_n": 36},
    },
    "regime_wide": {  # audit action 4 pre-registered: trend->wide, range->fast
        "signal_grid": {"stop_mult": 3.0, "breakeven_trigger_atr": 1.0,
                        "tp2_mult": 2.5, "tp3_mult": 3.0,
                        "regime_overrides": {
                            "trend_up": {"stop_mult": 4.0, "breakeven_trigger_atr": 1.0,
                                         "tp2_mult": 2.5, "tp3_mult": 4.0,
                                         "scaleout": {"tp1_ratio": 0.3, "tp2_ratio": 0.3}},
                            "trend_down": {"stop_mult": 4.0, "breakeven_trigger_atr": 1.0,
                                           "tp2_mult": 2.5, "tp3_mult": 4.0,
                                           "scaleout": {"tp1_ratio": 0.3, "tp2_ratio": 0.3}},
                            "range": {"stop_mult": 2.0, "breakeven_trigger_atr": 0.5,
                                      "scaleout": {"tp1_ratio": 0.6, "tp2_ratio": 0.4}},
                        }},
        "ensemble": {"min_confidence_to_alert": 0.80},
        "labeling": {"horizon_candles_n": 36},
    },
    "regime_fast": {  # audit action 4 pre-registered: early-BE everywhere;
        # regime_overrides: None cancels the shipped overrides (they made
        # regime_fast identical to current in the 2026-08-07 run).
        "signal_grid": {"stop_mult": 3.0, "breakeven_trigger_atr": 0.5,
                        "tp2_mult": 2.5, "tp3_mult": 3.0,
                        "regime_overrides": None},
        "ensemble": {"min_confidence_to_alert": 0.80},
        "labeling": {"horizon_candles_n": 36},
    },
    "null": None,
}

# Synthetic-demo fallback price scales (only used when no DB is available).
_SYNTH_DEFAULTS: dict = {
    "XAUUSD": dict(price=2400.0, atr=4.0, freq="5min"),
    "XAGUSD": dict(price=30.0, atr=0.25, freq="15min"),
    "BTCUSD": dict(price=50000.0, atr=1200.0, freq="5min"),
    "EURUSD": dict(price=1.08, atr=0.0012, freq="1h"),
    "GBPUSD": dict(price=1.28, atr=0.0014, freq="1h"),
}


# ---------------------------------------------------------------------------
# Synthetic demo data (tests / no-DB fallback) — NEVER used on real data.
# ---------------------------------------------------------------------------

def _make_synthetic_wf_df(n: int, price: float, atr: float, freq: str, seed: int = 123) -> pd.DataFrame:
    """Long synthetic OHLC series that produces walk-forward folds.

    The `ml_p_*` probs are injected afterwards with a deliberate
    look-ahead bias (see `_inject_biased_probs`) so the demo can show the
    machinery detecting a real edge; real runs never see this path.
    """
    np.random.seed(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq=freq, tz="UTC")
    t = np.arange(n)
    noise_scale = atr * 0.45
    trend = atr * (0.5 * np.sin(t / 400.0) + 0.25 * np.sin(t / 80.0))
    noise = np.cumsum(np.random.randn(n) * noise_scale)
    closes = price + trend + noise
    opens = closes + np.random.randn(n) * noise_scale * 0.3
    highs = np.maximum(opens, closes) + np.abs(np.random.randn(n)) * noise_scale * 0.55
    lows = np.minimum(opens, closes) - np.abs(np.random.randn(n)) * noise_scale * 0.55
    return pd.DataFrame({
        "timestamp_utc": to_epoch_seconds(idx),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": (1000 + np.random.randint(-300, 300, n)).astype(float),
        "session": np.random.choice(["london", "newyork", "asia"], n, p=[0.45, 0.35, 0.20]),
        "regime": np.random.choice(
            ["trend_up", "trend_down", "range", "compression"], n,
            p=[0.30, 0.30, 0.30, 0.10]),
        "atr": atr,
    })


def _inject_biased_probs(df: pd.DataFrame, strength: float = 0.30, seed: int = 7) -> pd.DataFrame:
    """SYNTHETIC ONLY: ML probs biased toward the future 6-bar move (leakage
    by design) so the demo pipeline provably detects a real edge. The real
    pipeline never uses this function."""
    rng = np.random.default_rng(seed)
    d = df.copy()
    future_move = (d["close"].shift(-6).fillna(d["close"]) - d["close"]) / (d["atr"] + 1e-9)
    bias = np.tanh(future_move * 1.8)
    d["ml_p_long"] = np.clip(0.5 + bias * strength + rng.normal(0.0, 0.06, len(d)), 0.05, 0.95)
    d["ml_p_short"] = 1.0 - d["ml_p_long"]
    return d


def _null_probs(n: int, seed: int = 123) -> np.ndarray:
    """Random 0.5 ± 0.05 probabilities — the no-information negative control."""
    rng = np.random.default_rng(seed)
    return np.clip(0.5 + rng.normal(0.0, 0.05, n), 0.05, 0.95)


# ---------------------------------------------------------------------------
# Variant plumbing
# ---------------------------------------------------------------------------

def _variants_for(asset_key: str) -> dict:
    if asset_key == "GBPUSD":
        return dict(GBP_VARIANTS)
    return dict(GENERIC_VARIANTS)


def _select_variants(asset_key: str, names: str | None) -> dict:
    family = _variants_for(asset_key)
    if not names:
        return family
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in family]
    if unknown:
        raise SystemExit(f"Unknown variant(s): {unknown}; available: {list(family)}")
    return {n: family[n] for n in wanted}


def _apply_variant(cfg: dict, asset_key: str, overrides: dict | None) -> dict:
    """Deep-copy cfg with the variant's per-asset section patches applied.

    A patch value of None REMOVES the key from the merged section (needed to
    cancel an inherited block, e.g. `signal_grid.regime_overrides: null` must
    strip the overrides the shipped config now carries, so variants like
    `legacy` are measured against the plain grid instead of silently
    inheriting the current config's per-regime policy).
    """
    cfg_v = copy.deepcopy(cfg)
    if not overrides:
        return cfg_v
    asset = cfg_v.setdefault("assets", {}).setdefault(asset_key, {})
    for section, patch in overrides.items():
        merged = copy.deepcopy(asset.get(section, {}))
        for k, v in patch.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        asset[section] = merged
    return cfg_v


def _apply_cost_mult(cfg: dict, asset_key: str, mult: float) -> dict:
    """Stress-test costs: multiply spread, slippage and commission by `mult`
    (audit: the strategy must survive 1.5x costs with PF > 1.1, or it is a bet
    on the broker not widening spreads). Per-asset overrides first, then the
    global backtest defaults."""
    cfg_v = copy.deepcopy(cfg)
    asset = cfg_v.setdefault("assets", {}).setdefault(asset_key, {})
    for key in ("spread_usd", "slippage_usd"):
        if key in asset:
            asset[key] = float(asset[key]) * mult
    bt = cfg_v.setdefault("backtest", {})
    bt["commission_per_trade"] = float(bt.get("commission_per_trade", 0.0)) * mult
    if "spread_points" in bt:
        bt["spread_points"] = float(bt["spread_points"]) * mult
    if "slippage_points" in bt:
        bt["slippage_points"] = float(bt["slippage_points"]) * mult
    return cfg_v


# ---------------------------------------------------------------------------
# Per-fold model scoring (real data only; MUST match run_backtest.strategy_fn)
# ---------------------------------------------------------------------------

def _score_fold(train_df: pd.DataFrame, test_df: pd.DataFrame, cfg: dict,
                asset_key: str) -> pd.DataFrame:
    """Train an XGBoost on the train window ONLY (temp-file model, never the
    production file) and return the test frame augmented with ml_p_*.

    Parity requirement (harness reconciliation 2026-08-14): this must score a
    fold the same way `scripts/run_backtest.strategy_fn_factory` does, or the
    decision gate and the reported backtest describe two different systems. On
    XAUUSD M15 with `--end-date 2026-08-08` they disagreed by 6x (318 trades /
    -2415.20 here versus 365 trades / -396.55 there) because this function used
    to fit without uniqueness sample weights, skip the per-asset model section
    and predict on `test_df.fillna(0.0)` — which scores warm-up rows whose
    features are NaN as valid 0.0-feature rows (0.0 is a meaningful value for a
    z-score feature) and opens trades the live trader can never take.

    Parity was confirmed on 2026-08-14: both harnesses now report 365 trades and
    -396.5 on XAUUSD, fold for fold.
    """
    cfg_inner = merge_asset_cfg(cfg, asset_key, "labeling")
    cfg_inner = merge_asset_cfg(cfg_inner, asset_key, "ensemble")
    # Per-asset model flags (use_regime_feature / include_zero_class) must reach
    # build_training_matrix, otherwise a per-asset 3-class model is measured as
    # the global binary one.
    cfg_inner = merge_asset_cfg(cfg_inner, asset_key, "model")
    cfg_inner = _maybe_downgrade_three_class(cfg_inner, train_df, asset_key)

    X_train, y_train, cols = build_training_matrix(train_df, cfg=cfg_inner)
    test_df_eval = test_df.copy()
    # Neutral 0.5/0.5 is the baseline for every row: it can never pass a filter,
    # so a fold we fail to model contributes no trades instead of a bogus signal.
    test_df_eval["ml_p_long"] = 0.5
    test_df_eval["ml_p_short"] = 0.5
    calibrated = None

    if len(X_train) >= 30 and y_train.nunique() >= 2:
        # Weight training rows by average label uniqueness so overlapping
        # horizon-labels do not over-represent the information that is actually
        # unique (Lopez de Prado, AFML ch.4). split_fold_frames has already
        # purged the tail rows whose labels reach into the test window; the
        # weights handle the residual overlap among the survivors.
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
            # Data condition, not a defect: degrade THIS fold to "no signal"
            # instead of aborting the whole multi-variant run.
            print(f"[dsr] WARNING: fold degraded to no-signal -- {exc}")
            calibrated = None

    if calibrated is None:
        return test_df_eval

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="wf_model_", suffix=".joblib")
    os.close(tmp_fd)
    try:
        save_model(calibrated, cols, tmp_path)
        predictor = ModelPredictor(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    try:
        # W13: rows with any NaN in the model's feature columns stay NEUTRAL
        # instead of being filled with 0.0, matching both the live trader and
        # run_backtest.strategy_fn.
        feat_cols = [c for c in cols if c in test_df_eval.columns]
        complete = test_df_eval[feat_cols].notna().all(axis=1) if feat_cols \
            else pd.Series(True, index=test_df_eval.index)
        if complete.any():
            preds = predictor.predict_proba(test_df_eval[complete])
            test_df_eval.loc[complete, "ml_p_long"] = preds["p_long"].values
            test_df_eval.loc[complete, "ml_p_short"] = preds["p_short"].values
    except Exception:
        test_df_eval["ml_p_long"] = 0.5
        test_df_eval["ml_p_short"] = 0.5
    return test_df_eval


def _build_fold_frames(df: pd.DataFrame, cfg: dict, asset_key: str,
                       max_folds: int | None) -> tuple[list, list]:
    """Slice walk-forward windows with the SHARED honest splitter and score them
    with per-fold models unless the frame already carries injected ml_p_*
    (synthetic demo).

    `split_fold_frames` receives the UNMERGED cfg on purpose: that is what
    `run_walk_forward` passes, so both harnesses purge with the same horizon.
    (A per-asset `labeling.horizon_candles_n` override is therefore ignored by
    the purge on BOTH paths — tracked separately; fixing it here alone would
    re-open the very divergence this commit closes.)
    """
    wf_cfg = cfg["backtest"]["walk_forward"]
    windows = generate_windows(
        df, wf_cfg["train_window_days"], wf_cfg["test_window_days"], wf_cfg["step_days"])
    if max_folds is not None and len(windows) > max_folds:
        windows = windows[:max_folds]
    secs = bar_seconds(df)
    frames = []
    for w in windows:
        train_df, test_df = split_fold_frames(df, cfg, w, bar_secs=secs)
        test_df = test_df.copy()
        if "ml_p_long" in df.columns:
            frames.append(test_df)
        else:
            frames.append(_score_fold(train_df, test_df, cfg, asset_key))
    return windows, frames


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _summarize_trial(name: str, fold_trades: list[np.ndarray], n_folds: int,
                     historical_trials: int, n_variants: int,
                     trades_per_year: float, n_eff_historical: float,
                     trade_r: list[float] | None = None,
                     horizon_bars: int = 36) -> dict:
    """One row of the DSR report for a single config variant."""
    if not fold_trades:
        return {"variant": name, "n_trades": 0, "t_eff": 0.0, "n_folds": n_folds,
                "traded_folds": 0, "valid_folds": 0, "pos_folds": 0,
                "median_fold_pnl": 0.0, "best_fold_pnl": 0.0,
                "total_pnl_ex_best": 0.0, "t_block": float("nan"),
                "total_pnl": 0.0, "expectancy": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "median_fold_pf": 0.0, "sharpe": 0.0,
                "skew": 0.0, "kurtosis_excess": 0.0, "psr_0": float("nan"),
                "dsr_trials": float("nan"), "dsr_historical": float("nan"),
                "min_trl_trades": float("inf"), "min_trl_years": float("inf")}
    arr = np.concatenate(fold_trades).astype(float)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

    fold_pfs = []
    for ft in fold_trades:
        if len(ft) >= 3:
            fold_pfs.append(compute_metrics(pd.DataFrame({"pnl": ft}))["profit_factor"])
    median_fold_pf = float(np.median(fold_pfs)) if fold_pfs else 0.0

    # Effective sample size T_eff from label/trade uniqueness
    uniqueness = average_uniqueness_weights(len(arr), horizon=horizon_bars) if len(arr) > 0 else np.array([])
    t_eff = float(np.sum(uniqueness)) if len(uniqueness) > 0 else float(len(arr))

    sr = annualized_sharpe(arr)
    psr_0 = probabilistic_sharpe_ratio(arr, sr_benchmark=0.0, t_eff=t_eff)
    d_trials = deflated_sharpe_ratio(arr, n_trials=n_variants, t_eff=t_eff)
    d_neff = deflated_sharpe_ratio(arr, n_trials=max(n_eff_historical, 1.0), t_eff=t_eff)
    d_hist = deflated_sharpe_ratio(arr, n_trials=historical_trials, t_eff=t_eff)
    mtrl = minimum_track_record_length(arr, n_trials=historical_trials, t_eff=t_eff)

    # A fold that traded at all vs a fold that traded ENOUGH to vote. Fold 9 of
    # the 2026-08-14 XAUUSD run held one trade (-122.24): its sign is noise, so
    # it must not count as a verdict in either direction.
    fold_sums = [float(ft.sum()) if len(ft) else 0.0 for ft in fold_trades]
    traded_folds = int(np.sum([len(ft) > 0 for ft in fold_trades]))
    valid_mask = [len(ft) >= MIN_TRADES_FOR_VALID_FOLD for ft in fold_trades]
    valid_folds = int(np.sum(valid_mask))
    valid_sums = [s for s, ok in zip(fold_sums, valid_mask) if ok]
    # A POSITIVE fold is a VALID fold that MADE MONEY. This used to be
    # `np.sum(ft > 0) > 0`, i.e. "the fold contains at least one winning
    # trade", which is true of essentially every non-empty fold - including
    # every fold of the random-probability `null` control (12/12).
    pos_folds = int(np.sum([s > 0.0 for s in valid_sums]))
    median_fold_pnl = float(np.median(valid_sums)) if valid_sums else 0.0
    # Concentration: how much of the result is one window? A family whose profit
    # disappears when the single best fold is removed has one good period, not
    # an edge. Only a POSITIVE best fold is removable (subtracting a negative
    # best fold would flatter the result instead of stressing it).
    best_fold_pnl = max(max(fold_sums), 0.0) if fold_sums else 0.0
    total_pnl = float(arr.sum())
    total_pnl_ex_best = total_pnl - best_fold_pnl
    t_block = block_bootstrap_t(trade_r) if trade_r and len(trade_r) >= 2 else float("nan")

    return {
        "variant": name,
        "n_trades": int(len(arr)),
        "t_eff": round(t_eff, 1),
        "n_folds": n_folds,
        "traded_folds": traded_folds,
        "valid_folds": valid_folds,
        "t_block": t_block,
        "pos_folds": pos_folds,
        "median_fold_pnl": round(median_fold_pnl, 2),
        "best_fold_pnl": round(best_fold_pnl, 2),
        "total_pnl_ex_best": round(total_pnl_ex_best, 2),
        "total_pnl": round(total_pnl, 2),
        "expectancy": round(float(arr.mean()), 4),
        "win_rate": round(100.0 * float(np.mean(arr > 0)), 1),
        "profit_factor": round(pf, 2) if pf != 999.0 else 999.0,
        "median_fold_pf": round(median_fold_pf, 2),
        "sharpe": round(sr, 3),
        "skew": round(float(d_trials["skew"]), 3),
        "kurtosis_excess": round(float(d_trials["kurtosis_excess"]), 3),
        "psr_0": round(psr_0, 4),
        "dsr_trials": round(float(d_trials["dsr"]), 4),
        "dsr_neff": round(float(d_neff["dsr"]), 4),
        "dsr_historical": round(float(d_hist["dsr"]), 4),
        "min_trl_trades": round(float(mtrl["min_trl_trades"]), 1),
        "min_trl_years": round(float(mtrl["min_trl_trades"] / trades_per_year), 2)
        if trades_per_year > 0 else float("inf"),
    }


def run_analysis(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                 variants: dict | None = None,
                 historical_trials: int = 729,
                 n_splits: int | None = None,
                 max_folds: int | None = None,
                 random_seed: int = 42,
                 cost_stress: bool = True) -> dict:
    """Run the walk-forward family comparison + DSR/CSCV for one asset.

    Returns a plain-python dict (JSON-serializable) with per-trial rows and
    the CSCV summary. Raises ValueError when the data cannot produce folds.
    """
    if variants is None:
        variants = _variants_for(asset_key)

    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(
            f"No walk-forward folds produced for {asset_key} "
            f"({len(df_full)} rows). Need >= train+test span "
            f"({cfg['backtest']['walk_forward']['train_window_days'] + cfg['backtest']['walk_forward']['test_window_days']} days).")

    span_secs = float(df_full["timestamp_utc"].max() - df_full["timestamp_utc"].min())
    years = span_secs / (86400.0 * 365.25) if span_secs > 0 else 1.0

    cur_cfg = _apply_variant(cfg, asset_key, variants.get("current"))
    cur_asset = cur_cfg["assets"].get(asset_key, {})
    sg = get_signal_grid(cur_cfg, cur_asset)
    merged_ens = merge_asset_cfg(cur_cfg, asset_key, "ensemble")["ensemble"]
    merged_lab = merge_asset_cfg(cur_cfg, asset_key, "labeling")["labeling"]

    fold_matrix = []
    trials = []
    current_r: list[float] = []
    n_eff_historical = float(len(variants))
    for name, overrides in variants.items():
        cfg_v = _apply_variant(cfg, asset_key, overrides)
        fold_trades: list[np.ndarray] = []
        variant_r: list[float] = []
        for fold_i, fdf in enumerate(frames):
            fdf_run = fdf
            if name == "null":
                fdf_run = fdf.copy()
                p = _null_probs(len(fdf_run), seed=random_seed + fold_i)
                fdf_run["ml_p_long"] = p
                fdf_run["ml_p_short"] = 1.0 - p
            cfg_run = merge_asset_cfg(cfg_v, asset_key, "labeling")
            cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
            engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
            trades = engine.run(fdf_run.reset_index(drop=True))
            tdf = trades_to_dataframe(trades)
            pnls = tdf["pnl"].to_numpy(dtype=float) if len(tdf) else np.array([], dtype=float)
            fold_trades.append(pnls)
            if name == "current":
                # Per-trade R of the current config (audit: gate needs an
                # honest block-bootstrap t, which requires trade-level R).
                for t in trades:
                    if getattr(t, "initial_stop_price", None):
                        risk = abs(t.entry_price - t.initial_stop_price) * t.volume * engine.point_value_lot
                        if risk > 1e-12:
                            variant_r.append(float(t.pnl / risk))
        n_total = int(sum(len(ft) for ft in fold_trades))
        trades_per_year = n_total / years if years > 0 else 0.0
        if name == "current":
            current_r = variant_r
        trials.append(_summarize_trial(
            name, fold_trades, len(windows), historical_trials,
            n_variants=len(variants), trades_per_year=trades_per_year,
            n_eff_historical=n_eff_historical, trade_r=variant_r,
            horizon_bars=int(merged_lab.get("horizon_candles_n", 36))))
        fold_matrix.append([float(ft.sum()) if len(ft) else 0.0 for ft in fold_trades])

    fold_arr = np.asarray(fold_matrix, dtype=float)
    cscv = cscv_pbo(fold_arr, n_splits=n_splits)

    # Effective number of trials: rho_bar from THIS family, extrapolated to the
    # full historical trial count (audit: publish DSR at N_eff AND at full N).
    n_eff_info = effective_number_trials(fold_arr)
    mean_rho = n_eff_info.get("mean_rho")
    pr_ratio = float(n_eff_info.get("participation_ratio", 1.0))
    if np.isfinite(mean_rho):
        n_eff_historical = 1.0 + (historical_trials - 1.0) * (1.0 - mean_rho)
    else:
        n_eff_historical = float(historical_trials)
    n_eff_historical = max(n_eff_historical, pr_ratio)

    # Cost stress (audit gate: survive 1.5x costs with PF > 1.1): rerun ONLY
    # the current variant with spread/slippage/commission x 1.5.
    stress = None
    if cost_stress and "current" in variants:
        cfg_s = _apply_cost_mult(cfg, asset_key, 1.5)
        cfg_s = _apply_variant(cfg_s, asset_key, variants["current"])
        stress_fold_pfs = []
        stress_pnls = []
        for fdf in frames:
            cfg_run = merge_asset_cfg(cfg_s, asset_key, "labeling")
            cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
            engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
            trades = engine.run(fdf.reset_index(drop=True))
            arr = np.array([t.pnl for t in trades], dtype=float)
            if len(arr) >= 3:
                wins, losses = arr[arr > 0], arr[arr <= 0]
                gp, gl = float(wins.sum()), float(-losses.sum())
                stress_fold_pfs.append((gp / gl) if gl > 0 else 999.0)
            stress_pnls.extend(arr.tolist())
        stress = {
            "cost_mult": 1.5,
            "n_trades": len(stress_pnls),
            "total_pnl": round(float(np.sum(stress_pnls)), 2) if stress_pnls else 0.0,
            "profit_factor": round(float(np.sum(np.maximum(stress_pnls, 0.0)) /
                                         max(-np.sum(np.minimum(stress_pnls, 0.0)), 1e-12)), 2)
            if stress_pnls else 0.0,
            "median_fold_pf": round(float(np.median(stress_fold_pfs)), 2) if stress_fold_pfs else None,
        }

    # Record the effective config of the "current" variant for the report.
    cur_cfg = _apply_variant(cfg, asset_key, variants.get("current"))
    cur_asset = cur_cfg["assets"].get(asset_key, {})
    sg = get_signal_grid(cur_cfg, cur_asset)
    merged_ens = merge_asset_cfg(cur_cfg, asset_key, "ensemble")["ensemble"]
    merged_lab = merge_asset_cfg(cur_cfg, asset_key, "labeling")["labeling"]

    return {
        "asset": asset_key,
        "n_folds": len(windows),
        "years": round(years, 2),
        "n_trials": len(variants),
        "historical_trials": historical_trials,
        "min_trades_for_valid_fold": MIN_TRADES_FOR_VALID_FOLD,
        "n_eff": {
            "family_rho_bar": round(float(mean_rho), 4) if np.isfinite(mean_rho) else None,
            "n_eff_historical": round(n_eff_historical, 2),
            "family_participation_ratio": round(n_eff_info.get("participation_ratio", float("nan")), 2),
        },
        "current_config": {
            "signal_grid": {k: v for k, v in sg.items()},
            "ensemble": {
                "min_confidence_to_alert": merged_ens.get("min_confidence_to_alert"),
                "ev_threshold": merged_ens.get("ev_threshold", 0),
                "hard_divergence_veto": merged_ens.get("hard_divergence_veto", False),
            },
            "labeling": {"horizon_candles_n": merged_lab.get("horizon_candles_n")},
        },
        "trials": trials,
        "cscv": cscv,
        "cost_stress": stress,
        "current_r": current_r,
    }


# ---------------------------------------------------------------------------
# Report + CLI
# ---------------------------------------------------------------------------

def _fmt(x, width: int = 9, digits: int = 2) -> str:
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return "n/a".rjust(width)
    if isinstance(x, float):
        return f"{x:.{digits}f}".rjust(width)
    return str(x).rjust(width)


def fold_health(cur: dict | None) -> dict:
    """Did the folds actually make money, or did they merely vote that way?

    Three legs, all required, none implied by the others:

    * `total_pnl > 0` — the family has to be profitable at all. A checklist
      that can be passed by a losing config is not a checklist.
    * `total_pnl - best_fold > 0` — the result must survive deleting its single
      best window. This is the leg a fat right tail cannot buy.
    * `>= 55% of VALID folds positive` — the original condition, kept, but now
      computed over folds with at least MIN_TRADES_FOR_VALID_FOLD trades.

    NOTE on a leg that is NOT here: "median fold PnL > 0" was tried first and
    dropped, because it cannot fail while the 55% leg passes (if strictly more
    than half of the folds are positive, their median is positive by
    construction). `median_fold_pnl` is still reported as a description.

    The 2026-08-14 XAUUSD run passed the third leg (3/5 = 60% of valid folds)
    with -396.5 total and -1514.1 ex-best, which is precisely why the other two
    exist.
    """
    if cur is None:
        return {"total_pnl": 0.0, "total_pnl_positive": False,
                "total_pnl_ex_best": 0.0, "ex_best_positive": False,
                "best_fold_pnl": 0.0, "median_fold_pnl": 0.0,
                "pos_folds": 0, "valid_folds": 0, "positive_share": 0.0,
                "positive_share_ok": False, "passed": False}
    total = float(cur.get("total_pnl", 0.0))
    ex_best = float(cur.get("total_pnl_ex_best", 0.0))
    valid = int(cur.get("valid_folds", 0))
    pos = int(cur.get("pos_folds", 0))
    share = (pos / valid) if valid > 0 else 0.0
    total_ok = total > 0.0
    ex_best_ok = ex_best > 0.0
    share_ok = bool(valid > 0 and share >= POS_FOLD_SHARE_MIN)
    return {
        "total_pnl": total,
        "total_pnl_positive": total_ok,
        "total_pnl_ex_best": ex_best,
        "ex_best_positive": ex_best_ok,
        "best_fold_pnl": float(cur.get("best_fold_pnl", 0.0)),
        "median_fold_pnl": float(cur.get("median_fold_pnl", 0.0)),
        "pos_folds": pos,
        "valid_folds": valid,
        "positive_share": share,
        "positive_share_ok": share_ok,
        "passed": bool(total_ok and ex_best_ok and share_ok),
    }


def decision_gate(res: dict) -> dict:
    """Hard admission checklist for live capital (audit, Claude 5 Opus plan).

    All conditions simultaneously:
      1. block-bootstrap t >= 3.0 on R-multiplicators
      2. DSR > 0.95 at the defensible N_eff
      3. PBO < 0.20 (the report calls 0.20-0.30 HIGH overfit risk, so the gate
         must not admit inside that band)
      4. survives 1.5x costs with PF > 1.1
      5. folds: total PnL > 0 AND total PnL without the best fold > 0 AND
         >= 55% of valid folds positive (see `fold_health`)
      6. IS->OOS slope >= 0.5
      7. locked hold-out confirms (not computable here — always 'pending')
    Returns {checks, fold_health, passed_all}.
    """
    cur = next((t for t in res["trials"] if t["variant"] == "current"), None)
    cscv = res["cscv"]
    fh = fold_health(cur)
    checks = {
        "block_bootstrap_t >= 3.0": bool(cur is not None and cur.get("t_block", float("nan")) >= 3.0),
        "DSR(N_eff) > 0.95": bool(cur is not None and cur.get("dsr_neff", float("nan")) > 0.95),
        PBO_CONDITION: bool(cscv["pbo"] < PBO_MAX),
        "PF > 1.1 at 1.5x costs": bool(res.get("cost_stress") and res["cost_stress"]["profit_factor"] > 1.1),
        FOLD_CONDITION: bool(fh["passed"]),
        "IS->OOS slope >= 0.5": bool(cscv.get("is_oos_slope") is not None
                                     and np.isfinite(cscv.get("is_oos_slope", float("nan")))
                                     and cscv["is_oos_slope"] >= 0.5),
        "locked hold-out confirms": None,  # organizational, set by the user
    }
    known = [v for v in checks.values() if v is not None]
    return {"checks": checks, "fold_health": fh, "passed_all": bool(known) and all(known)}


def print_report(res: dict) -> None:
    a = res["asset"]
    print(f"\n=== Deflated Sharpe / CSCV: {a} ===")
    print(f"Walk-forward: {res['n_folds']} folds over ~{res['years']} years | "
          f"trials in family: {res['n_trials']} | historical trials (deflation): "
          f"{res['historical_trials']}")
    if res.get("end_date"):
        print(f"Sample truncated at {res['end_date']} (locked hold-out NOT touched)")
    cc = res["current_config"]
    print("Current config: grid=" + str({k: v for k, v in cc["signal_grid"].items()
                                         if v is not None}) +
          f" | conf={cc['ensemble']['min_confidence_to_alert']} | "
          f"h={cc['labeling']['horizon_candles_n']}")

    neff = res.get("n_eff", {})
    rho = neff.get("family_rho_bar")
    rho_s = f"{rho:.2f}" if rho is not None else "n/a"
    print(f"Trial correlation: rho_bar={rho_s} -> N_eff({res['historical_trials']}) = "
          f"{neff.get('n_eff_historical', 'n/a')} (participation ratio "
          f"{neff.get('family_participation_ratio', 'n/a')})")
    print(f"+folds = valid folds with positive PnL / valid folds "
          f"(valid = >= {MIN_TRADES_FOR_VALID_FOLD} trades); "
          f"exBest = total PnL minus the single best fold")

    dsr_label = f"DSR({res['historical_trials']})"
    neff_label = f"DSR(Nef)"
    hdr = (f"{'variant':<14}{'n_tr':>6}{'PnL':>9}{'exBest':>9}{'WR%':>6}{'PF':>6}"
           f"{'medPF':>7}{'SR':>8}{'PSR(0)':>8}{'DSR(n)':>8}"
           f"{neff_label:>8}{dsr_label:>9}{'MinTRL y':>9}{'+folds':>8}")
    print(hdr)
    print("-" * len(hdr))
    for t in res["trials"]:
        print(f"{t['variant']:<14}{t['n_trades']:>6}{t['total_pnl']:>9.1f}"
              f"{t.get('total_pnl_ex_best', 0.0):>9.1f}"
              f"{t['win_rate']:>6.1f}{t['profit_factor']:>6.2f}{t['median_fold_pf']:>7.2f}"
              f"{t['sharpe']:>8.2f}{_fmt(t['psr_0'], 8):>8}{_fmt(t['dsr_trials'], 8):>8}"
              f"{_fmt(t['dsr_neff'], 8):>8}"
              f"{_fmt(t['dsr_historical'], 9):>9}{_fmt(t['min_trl_years'], 9):>9}"
              f"{str(t['pos_folds']) + '/' + str(t.get('valid_folds', 0)):>8}")

    c = res["cscv"]
    print(f"\nCSCV (Probability of Backtest Overfitting):")
    print(f"  splits={c['n_splits']} (blocks of {c['n_observations'] // c['n_splits']} folds), "
          f"combinations={c['n_combinations']} (of {c['total_combinations']})")
    print(f"  PBO = {c['pbo']:.3f} | mean lambda = {c['mean_lambda']:+.3f} | "
          f"median lambda = {c['median_lambda']:+.3f} | frac lambda>0 = "
          f"{c['frac_lambda_positive']:.3f} | OOS prob loss = {c['oos_prob_loss']:.3f} | "
          f"IS->OOS degradation = {c['is_oos_degradation']:+.2%}")
    if c["pbo"] <= 0.10:
        verdict = "LOW overfit risk: the IS-best config usually wins OOS."
    elif c["pbo"] <= 0.20:
        verdict = "MODERATE overfit risk: selection is informative but noisy."
    else:
        verdict = "HIGH overfit risk: selection procedure considered overfit."
    print(f"  Verdict: {verdict}")
    slope = c.get("is_oos_slope")
    if slope is not None and np.isfinite(slope):
        print(f"  IS->OOS slope = {slope:.2f} ({'informative' if slope >= 0.5 else 'weak/overfit'})")

    # Cost stress + decision gate
    st = res.get("cost_stress")
    if st:
        print(f"\nCost stress x{st['cost_mult']}: n={st['n_trades']} PnL={st['total_pnl']} "
              f"PF={st['profit_factor']} median fold PF={st['median_fold_pf']} "
              f"({'PASS' if st['profit_factor'] > 1.1 else 'FAIL'} PF>1.1)")
    gate = decision_gate(res)
    print("\nDecision gate (all conditions simultaneously):")
    for cond, ok in gate["checks"].items():
        if ok is None:
            print(f"  [ ] {cond}  (user/organizational)")
        else:
            print(f"  [{'x' if ok else ' '}] {cond}")
        if cond == FOLD_CONDITION:
            fh = gate["fold_health"]
            print(f"        total PnL {fh['total_pnl']:+.1f} "
                  f"[{'ok' if fh['total_pnl_positive'] else 'FAIL'}] | "
                  f"ex-best {fh['total_pnl_ex_best']:+.1f} "
                  f"(best fold {fh['best_fold_pnl']:+.1f}) "
                  f"[{'ok' if fh['ex_best_positive'] else 'FAIL'}] | "
                  f"{fh['pos_folds']}/{fh['valid_folds']} positive "
                  f"({100.0 * fh['positive_share']:.1f}%, need "
                  f"{100.0 * POS_FOLD_SHARE_MIN:.0f}%) "
                  f"[{'ok' if fh['positive_share_ok'] else 'FAIL'}]")
    print(f"  => {'PASS -> capital' if gate['passed_all'] else 'FAIL -> paper/shadow only'}")

    cur = next((t for t in res["trials"] if t["variant"] == "current"), None)
    if cur is not None:
        dsr = cur["dsr_historical"]
        if math.isnan(dsr):
            dsr_verdict = "no trades — nothing to deflate"
        elif dsr >= 0.95:
            dsr_verdict = "edge survives the 729-trial deflation (DSR >= 0.95)"
        elif dsr >= 0.50:
            dsr_verdict = "indistinguishable from best-of-729 luck (0.50 <= DSR < 0.95)"
        else:
            dsr_verdict = "worse than the best-of-729 null (DSR < 0.50)"
        print(f"  Current-config DSR({res['historical_trials']}): {dsr:.3f} -> {dsr_verdict}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Deflated Sharpe / CSCV assessment for one asset's config family.")
    parser.add_argument("--asset", required=True, help="Internal asset key (XAUUSD, ...)")
    parser.add_argument("--timeframe", default=None, help="Override timeframe (default: per-asset)")
    parser.add_argument("--db-path", default=None, help="SQLite DB (default: config general.db_path)")
    parser.add_argument("--variants", default=None,
                        help="Comma-separated variant subset (default: full family)")
    parser.add_argument("--historical-trials", type=int, default=None,
                        help="Total trials ever tried on this asset's data (deflation N); "
                             "default: max(trial-journal count, 729)")
    parser.add_argument("--n-splits", type=int, default=None, help="CSCV split count (default: auto)")
    parser.add_argument("--max-folds", type=int, default=None, help="Cap folds (quick runs/tests)")
    parser.add_argument("--no-cost-stress", action="store_true",
                        help="Skip the 1.5x-cost stress rerun of the current config")
    parser.add_argument("--no-journal", action="store_true",
                        help="Do not append this run to logs/trial_journal.csv")
    parser.add_argument("--allow-locked", action="store_true",
                        help="Allow test windows overlapping the locked hold-out")
    parser.add_argument("--end-date", default=None,
                        help="Drop candles at or after this UTC date (YYYY-MM-DD) before "
                             "building features, so the gate can be computed without "
                             "burning the locked hold-out. Same semantics as "
                             "scripts/run_backtest.py --end-date.")
    parser.add_argument("--out", default=None, help="Output CSV path (default: logs/deflated_sharpe_<asset>.csv)")
    args = parser.parse_args(argv)

    cfg = load_config()
    assets = cfg.get("assets", {})
    if args.asset not in assets:
        raise SystemExit(f"Unknown asset: {args.asset}")

    asset_cfg = assets[args.asset]
    timeframe = args.timeframe or asset_cfg.get("timeframe") or "M5"
    db_path = args.db_path or cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    synthetic = False
    try:
        raw = load_asset_history(db_path, timeframe, args.asset)
        # Same cut as run_backtest: applied to the RAW frame so no rolling
        # feature window absorbs a post-cutoff candle.
        if args.end_date:
            raw = truncate_before(raw, args.end_date, args.asset)
        df = build_full_df(cfg, raw, db_path=db_path, asset_key=args.asset)
        print(f"[dsr] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[dsr] WARNING: cannot load real data ({exc.__class__.__name__}: {exc})")
        print("[dsr] Falling back to SYNTHETIC demo data — results are NOT real.")
        if args.end_date:
            print("[dsr] NOTE: --end-date does not apply to synthetic data.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)  # ~1500 days of bars, capped
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    from scripts.trial_journal import default_historical_trials, enforce_locked_holdout, log_trial
    if args.historical_trials is None:
        args.historical_trials = default_historical_trials(args.asset)

    from backtest.walk_forward import generate_windows
    windows_probe = generate_windows(
        df, cfg["backtest"]["walk_forward"]["train_window_days"],
        cfg["backtest"]["walk_forward"]["test_window_days"],
        cfg["backtest"]["walk_forward"]["step_days"])
    enforce_locked_holdout(cfg, windows_probe, "deflated_sharpe", allow=args.allow_locked)

    variants = _select_variants(args.asset, args.variants)
    try:
        res = run_analysis(cfg, args.asset, df, variants=variants,
                           historical_trials=args.historical_trials,
                           n_splits=args.n_splits, max_folds=args.max_folds,
                           cost_stress=not args.no_cost_stress)
    except ValueError as exc:
        raise SystemExit(f"[dsr] {exc}")

    res["synthetic"] = synthetic
    res["end_date"] = args.end_date
    print_report(res)

    os.makedirs("logs", exist_ok=True)
    out_csv = args.out or f"logs/deflated_sharpe_{args.asset.lower()}.csv"
    pd.DataFrame(res["trials"]).to_csv(out_csv, index=False)

    # Append-only trial journal (audit: DSR's N must come from the journal).
    if not args.no_journal:
        log_trial(
            experiment="deflated_sharpe",
            asset=args.asset,
            params={"variants": args.variants or "all",
                    "historical_trials": args.historical_trials,
                    "end_date": args.end_date},
            metrics={"dsr_historical": next((t["dsr_historical"] for t in res["trials"]
                                             if t["variant"] == "current"), None),
                     "pbo": res["cscv"]["pbo"],
                     "n_eff": res["n_eff"]["n_eff_historical"]})
    out_json = out_csv.replace(".csv", ".json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)
    print(f"\n[drs] CSV -> {out_csv}")
    print(f"[drs] JSON -> {out_json}")


if __name__ == "__main__":
    main()
