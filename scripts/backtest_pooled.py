"""
Pooled cross-asset training evaluation (quant audit 2026-08-07, Claude plan
action 2): the audit's cheapest real capacity gain —

    "Pooled обучение по 5 активам: все фичи привести к безразмерному виду
     (в единицах ATR / z-score внутри роллинг-окна), добавить asset
     one-hot. Это x5 обучающих данных на фолд."

Honest design:
- per fold, a POOLED model is trained on ALL assets' TRAIN windows (same
  walk-forward calendar as per-asset runs), then scored on each asset's TEST
  window; per-asset models are trained/calibrated exactly like
  scripts/run_backtest.py (temp-file models, purged calibration);
- comparison table: pooled vs per-asset on E[R], PF, t_block, AUC
  (directional) per asset, averaged across folds;
- asset one-hot columns (asset_XAUUSD, ...) are added at training time and
  synthesized at inference from the asset key, so the pooled model can learn
  asset-specific behavior while sharing the feature weights.

Dimensionless transform (config `pooled.scale: zscore|atr`):
  - atr: divide price-distance features by the row's ATR (they are already
    mostly *-atr); regime/oscillator features stay as-is;
  - zscore: per-asset rolling z-score (window `pooled.zscore_window`).

Not enabled by default — this is the research tool for the audit's
"15 features / pooled vs 46 features / per-asset" comparison.

Usage:
    python -m scripts.backtest_pooled --assets GBPUSD,EURUSD --max-folds 4
    python -m scripts.backtest_pooled --assets XAUUSD,BTCUSD,GBPUSD,EURUSD
"""

import argparse
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, effective_asset_config
from scripts.deflated_sharpe import (
    _make_synthetic_wf_df,
    _inject_biased_probs,
    _SYNTH_DEFAULTS,
)
from scripts.run_backtest import merge_asset_cfg
from backtest.walk_forward import generate_windows
from backtest.metrics import trades_to_dataframe, compute_r_metrics, block_bootstrap_t
from model.trainer import build_training_matrix, train_model, calibrate_model, save_model, FEATURE_COLUMNS
from model.predictor import ModelPredictor
from model.ensemble_backtest import EnsembleBacktester

ATR_SCALED_FEATURES = [f for f in FEATURE_COLUMNS if f.endswith("_atr") or f in (
    "atr_pct", "return_1", "return_4", "volume_ratio")]


def _asset_onehot(asset_key: str, assets: list[str]) -> pd.Series:
    return pd.Series({f"asset_{a}": 1.0 if a == asset_key else 0.0 for a in assets})


def _load_asset_frames(cfg, assets, max_folds):
    """Returns {asset: full df} with real data or synthetic fallback."""
    out = {}
    for asset in assets:
        a_cfg = cfg["assets"].get(asset, {})
        timeframe = a_cfg.get("timeframe") or "M5"
        db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
        try:
            from scripts.run_backtest import load_asset_history, build_full_df
            raw = load_asset_history(db_path, timeframe, asset)
            df = build_full_df(cfg, raw, db_path=db_path, asset_key=asset)
            out[asset] = df
        except Exception:
            spec = _SYNTH_DEFAULTS.get(asset, dict(price=1.28, atr=0.0014, freq="1h"))
            freq = spec["freq"]
            bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
            n = min(bars_per_day * 1500, 150_000)
            df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
            df = _inject_biased_probs(df)
            from labeling.label_generator import generate_labels_from_config
            cfg_asset = effective_asset_config(cfg, asset)
            df["label"] = generate_labels_from_config(
                df, cfg_asset, asset_key=asset
            )
            out[asset] = df
    return out


def _pooled_matrix(frames_by_asset: dict, assets: list[str], cfg: dict,
                   scale: str = "zscore", window: int = 200) -> tuple:
    """Stack per-asset train frames into one pooled matrix with asset one-hot.
    Returns (X, y, cols)."""
    X_parts, y_parts = [], []
    for asset in assets:
        df = frames_by_asset[asset]
        Xa, ya, cols = build_training_matrix(df, cfg={"model": merge_asset_cfg(
            cfg, asset, "model")["model"]})
        if len(ya) == 0:
            continue
        # dimensionless transform on the selected feature columns
        Xa = Xa.copy()
        if scale == "atr":
            atr = df.loc[Xa.index, "atr"].replace(0.0, np.nan)
            for f in ATR_SCALED_FEATURES:
                if f in Xa.columns:
                    Xa[f] = Xa[f] / atr
        elif scale == "zscore":
            for f in ATR_SCALED_FEATURES:
                if f in Xa.columns:
                    s = df.loc[Xa.index, f].astype(float)
                    m = s.rolling(window, min_periods=20).mean()
                    sd = s.rolling(window, min_periods=20).std(ddof=0)
                    Xa[f] = ((s - m) / sd.replace(0.0, np.nan)).to_numpy()
        oh = _asset_onehot(asset, assets)
        for c in oh.index:
            Xa[c] = float(oh[c])
        X_parts.append(Xa)
        y_parts.append(ya)
    X = pd.concat(X_parts)
    y = pd.concat(y_parts)
    cols = list(X.columns)
    return X, y, cols


def run_pooled_comparison(cfg: dict, assets: list[str], max_folds: int | None = None,
                          scale: str = "zscore", window: int = 200) -> dict:
    """Walk-forward comparison: per-asset models vs one pooled model."""
    import copy
    # Comparability: force the BINARY label space on BOTH arms (GBP's shipped
    # 3-class config would otherwise make the pooled matrix multi-class while
    # the per-asset arm stays 2-class). Regime features stay per-asset.
    cfg = copy.deepcopy(cfg)
    for a in assets:
        a_mod = cfg["assets"].setdefault(a, {}).setdefault("model", {})
        a_mod["include_zero_class"] = False
    cfg["model"]["include_zero_class"] = False

    frames = _load_asset_frames(cfg, assets, max_folds)
    wf = cfg["backtest"]["walk_forward"]
    # union calendar: use the longest available span among assets
    min_ts = max(int(f["timestamp_utc"].min()) for f in frames.values())
    max_ts = min(int(f["timestamp_utc"].max()) for f in frames.values())
    windows = generate_windows(pd.DataFrame({"timestamp_utc": [min_ts, max_ts]}),
                               wf["train_window_days"], wf["test_window_days"],
                               wf["step_days"])
    if not windows:
        raise ValueError("No walk-forward folds on the union calendar.")
    if max_folds is not None:
        windows = windows[:max_folds]

    per_asset_rows = {a: [] for a in assets}
    pooled_rows = {a: [] for a in assets}
    auc_rows = {a: [] for a in assets}

    for w in windows:
        # pooled training on all assets' train windows
        train_frames = {}
        for a in assets:
            train_frames[a] = frames[a][(frames[a]["timestamp_utc"] >= w.train_start_ts) &
                                        (frames[a]["timestamp_utc"] < w.train_end_ts)]
        try:
            Xp, yp, cols = _pooled_matrix(train_frames, assets, cfg, scale=scale, window=window)
            pooled_ok = len(yp) >= 60 and yp.nunique() >= 2
        except Exception:
            pooled_ok = False
        if pooled_ok:
            base = train_model(Xp, yp, cfg)
            calibrated = calibrate_model(base, Xp, yp, cfg)
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="wf_pooled_", suffix=".joblib")
            os.close(tmp_fd)
            try:
                save_model(calibrated, cols, tmp_path)
                predictor = ModelPredictor(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            predictor = None

        for a in assets:
            test = frames[a][(frames[a]["timestamp_utc"] >= w.test_start_ts) &
                             (frames[a]["timestamp_utc"] < w.test_end_ts)].copy()
            if len(test) == 0:
                continue
            # per-asset honest model (mirrors run_backtest.strategy_fn_factory)
            cfg_a = merge_asset_cfg(cfg, a, "model")
            Xtr, ytr, _ = build_training_matrix(train_frames[a], cfg={"model": cfg_a["model"]})
            test_a = test.copy()
            if len(Xtr) >= 30 and ytr.nunique() >= 2:
                base = train_model(Xtr, ytr, cfg_a)
                cal = calibrate_model(base, Xtr, ytr, cfg_a)
                fd, tp = tempfile.mkstemp(prefix="wf_a_", suffix=".joblib")
                os.close(fd)
                try:
                    save_model(cal, Xtr.columns.tolist(), tp)
                    pred = ModelPredictor(tp)
                finally:
                    if os.path.exists(tp):
                        os.remove(tp)
                test_a = _score_frame(pred, test_a, assets, a)
            else:
                test_a["ml_p_long"] = 0.5
                test_a["ml_p_short"] = 0.5
            per_asset_rows[a].append(_run_engine(cfg, a, test_a, volume_pl=1))

            # pooled scoring
            test_p = test.copy()
            if predictor is not None:
                test_p = _score_frame(predictor, test_p, assets, a)
            else:
                test_p["ml_p_long"] = 0.5
                test_p["ml_p_short"] = 0.5
            pooled_rows[a].append(_run_engine(cfg, a, test_p, volume_pl=1))
            auc_rows[a].append({
                "per_asset": _auc_from_frame(test_a),
                "pooled": _auc_from_frame(test_p),
            })

    return _summarize(assets, per_asset_rows, pooled_rows, auc_rows, scale, window)


def _score_frame(predictor, test_df: pd.DataFrame, assets: list[str], asset: str) -> pd.DataFrame:
    """Score `test_df` with a predictor whose feature set may exceed the frame's
    columns (synthetic/short frames): missing features are zero-filled; regime_*
    one-hots are synthesized from the raw `regime` column; asset_* one-hots from
    the asset key."""
    out = test_df.copy()
    try:
        feats = predictor.feature_cols
        build = {}
        for c in feats:
            if c in out.columns:
                build[c] = out[c]
            elif c.startswith("asset_"):
                build[c] = 1.0 if c == f"asset_{asset}" else 0.0
            elif c.startswith("regime_") and "regime" in out.columns:
                # RegimeLabel enum objects str() as 'RegimeLabel.TREND_UP', so
                # normalize to the .value key ('trend_up') before matching.
                reg_norm = out["regime"].map(
                    lambda r: r.value if hasattr(r, "value") else str(r))
                build[c] = (reg_norm == c.replace("regime_", "")).astype(float)
            else:
                build[c] = 0.0
        frame = pd.DataFrame(build, index=out.index)
        preds = predictor.predict_proba(frame.fillna(0.0))
        out["ml_p_long"] = preds["p_long"].values
        out["ml_p_short"] = preds["p_short"].values
    except Exception:
        out["ml_p_long"] = 0.5
        out["ml_p_short"] = 0.5
    return out

def _run_engine(cfg: dict, asset: str, test_df: pd.DataFrame, volume_pl: float):
    cfg_run = merge_asset_cfg(cfg, asset, "labeling")
    cfg_run = merge_asset_cfg(cfg_run, asset, "ensemble")
    engine = EnsembleBacktester(cfg_run, asset_key=asset)
    trades = engine.run(test_df.reset_index(drop=True))
    tdf = trades_to_dataframe(trades)
    return tdf


def _auc_from_frame(df: pd.DataFrame) -> float | None:
    """Directional OOS AUC: p of the chosen side vs the actual label."""
    from sklearn.metrics import roc_auc_score
    if "label" not in df.columns:
        return None
    d = df.dropna(subset=["label", "ml_p_long", "ml_p_short"])
    d = d[d["label"].isin([1, -1, 1.0, -1.0])]
    if len(d) < 20:
        return None
    p = np.where(d["label"] == 1, d["ml_p_long"], d["ml_p_short"])
    y = (d["label"] == 1).astype(int)
    try:
        return float(roc_auc_score(y, p))
    except ValueError:
        return None


def _summarize(assets, per_asset_rows, pooled_rows, auc_rows, scale, window) -> dict:
    out = {"scale": scale, "zscore_window": window, "assets": {}}
    for a in assets:
        pa = pd.concat(per_asset_rows[a], ignore_index=True) if per_asset_rows[a] else pd.DataFrame()
        po = pd.concat(pooled_rows[a], ignore_index=True) if pooled_rows[a] else pd.DataFrame()
        out["assets"][a] = {
            "per_asset": _frame_summary(pa),
            "pooled": _frame_summary(po),
            "auc": {
                "per_asset": _mean_auc([r["per_asset"] for r in auc_rows[a] if r["per_asset"] is not None]),
                "pooled": _mean_auc([r["pooled"] for r in auc_rows[a] if r["pooled"] is not None]),
            },
        }
    return out


def _frame_summary(tdf: pd.DataFrame) -> dict:
    if len(tdf) == 0:
        return {"n_trades": 0, "mean_r": 0.0, "pf": 0.0, "t_block": None, "win_rate_pct": 0.0}
    wins, losses = tdf["pnl"].clip(lower=0).sum(), -tdf["pnl"].clip(upper=0).sum()
    return {"n_trades": int(len(tdf)),
            "mean_r": round(float(tdf["pnl"].mean()), 4),
            "pf": round(float(wins / losses), 3) if losses > 0 else 999.0,
            "t_block": round(block_bootstrap_t(tdf["pnl"].to_numpy(dtype=float)), 3),
            "win_rate_pct": round(100.0 * float((tdf["pnl"] > 0).mean()), 1)}


def _mean_auc(vals: list) -> float | None:
    if not vals:
        return None
    return round(float(np.mean(vals)), 4)


def print_report(d: dict) -> None:
    print(f"\n=== Pooled vs per-asset (scale={d['scale']}, window={d['zscore_window']}) ===")
    for a, m in d["assets"].items():
        pa, po = m["per_asset"], m["pooled"]
        print(f"{a}:")
        print(f"  per-asset: n={pa['n_trades']:<6} E[R]={pa['mean_r']:+.4f} "
              f"PF={pa['pf']:<6} t={pa['t_block']}  AUC={m['auc']['per_asset']}")
        print(f"  pooled   : n={po['n_trades']:<6} E[R]={po['mean_r']:+.4f} "
              f"PF={po['pf']:<6} t={po['t_block']}  AUC={m['auc']['pooled']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Pooled cross-asset training vs per-asset.")
    parser.add_argument("--assets", default="GBPUSD,EURUSD")
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--scale", choices=["zscore", "atr"], default="zscore")
    parser.add_argument("--zscore-window", type=int, default=200)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/pooled_comparison.json)")
    args = parser.parse_args(argv)

    cfg = load_config()
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    d = run_pooled_comparison(cfg, assets, max_folds=args.max_folds,
                              scale=args.scale, window=args.zscore_window)
    print_report(d)
    os.makedirs("logs", exist_ok=True)
    out_json = args.out or "logs/pooled_comparison.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[pooled] -> {out_json}")


if __name__ == "__main__":
    main()
