"""Consolidated calibration report across the 5-asset portfolio.

For every model file in output/models (XAU/XAG/BTC/EUR/GBP production paths)
computes, on the SAME OOS-window discipline (last 1000 labeled rows after a
purge gap, production data path):

  * calibrator type / sigmoid a_, b_ (or isotonic, n folds)
  * std_p, mean |p-0.5| (sharpness)
  * p5 / p95 / min / max (probability spread)
  * share of predictions in [0.5, 0.6]  (the "collapse band")
  * ECE + Brier on the OOS tail
  * coverage at p>=0.55 / p>=0.60

Usage:
    python -m scripts.calibration_portfolio_report [--oos-bars 1000]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np

from config.loader import load_config, resolve_asset_timeframe
from model.calibration import compute_brier_score, compute_ece
from scripts.retrain_with_real_trades import read_candles
from scripts.train_mt5 import build_full_df, build_training_matrix

ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]


def _calibrator_desc(model) -> str:
    try:
        ccs = model.calibrated_classifiers_
        cal = ccs[0].calibrators[0]
        kind = type(cal).__name__
        if "Isotonic" in kind:
            return f"isotonic x{len(ccs)}"
        a_ = getattr(cal, "a_", float("nan"))
        b_ = getattr(cal, "b_", float("nan"))
        return f"sigmoid a={a_:.3f} b={b_:.3f}"
    except Exception:
        return "raw/no-cal"


def _collapsed(p: np.ndarray) -> float:
    return float(((p >= 0.5) & (p <= 0.6)).mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos-bars", type=int, default=1000)
    args = ap.parse_args()

    cfg = load_config()
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    oos_bars = args.oos_bars
    horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 24))

    rows = []
    print(f"{'asset':<8}{'tf':<5}{'calibrator':<26}{'std_p':>8}{'|p-.5|':>8}"
          f"{'p5':>7}{'p95':>7}{'min':>7}{'max':>7}{'in[.5,.6]':>10}{'ECE':>8}{'Brier':>8}"
          f"{'cov55':>7}{'cov60':>7}")
    for asset in ASSETS:
        acfg = cfg["assets"][asset]
        tf = resolve_asset_timeframe(cfg, asset)
        mp = Path(acfg["model_path"])
        if not mp.exists():
            rows.append((asset, tf, "MISSING", float("nan"), float("nan"), float("nan"),
                         float("nan"), float("nan"), float("nan"), float("nan"),
                         float("nan"), float("nan"), float("nan"), float("nan")))
            continue
        bundle = joblib.load(mp)
        model = bundle["model"]
        desc = _calibrator_desc(model)
        model_cols = bundle.get("feature_cols", [])

        raw = read_candles(db_path, tf, asset)
        if raw is None or raw.empty:
            rows.append((asset, tf, f"{desc} NO-DATA", float("nan"), float("nan"),
                         float("nan"), float("nan"), float("nan"), float("nan"),
                         float("nan"), float("nan"), float("nan"), float("nan")))
            continue
        full = build_full_df(raw, cfg, db_path, asset, tf)
        X_all, y_all, _av = build_training_matrix(full, cfg=cfg)
        # Align to the model's saved feature_cols (may include regime one-hots
        # the matrix builder did not synthesize without use_regime_feature).
        missing = [c for c in model_cols if c not in X_all.columns]
        if missing and "regime" in full.columns:
            from regime.classifier import regime_onehot_df
            oh = regime_onehot_df(full)
            new_cols = [c for c in oh.columns if c in missing]
            X_all = X_all.join(oh[new_cols])
        if all(c in X_all.columns for c in model_cols):
            X_all = X_all[model_cols]
        gap = max(horizon, int(cfg.get("model", {}).get("purge_gap_bars", 36)))
        n = len(X_all)
        oos_start = max(0, n - oos_bars)
        if oos_start <= 0:
            rows.append((asset, tf, f"{desc} SHORT", float("nan"), float("nan"),
                         float("nan"), float("nan"), float("nan"), float("nan"),
                         float("nan"), float("nan"), float("nan"), float("nan")))
            continue
        X_o = X_all.iloc[oos_start:]
        y_o = y_all.iloc[oos_start:]
        p = np.asarray(model.predict_proba(X_o), dtype=float)[:, 1]
        ece, _ = compute_ece(y_o, p)
        brier = compute_brier_score(y_o, p)
        row = (
            asset, tf, desc,
            float(np.std(p)), float(np.mean(np.abs(p - 0.5))),
            float(np.percentile(p, 5)), float(np.percentile(p, 95)),
            float(p.min()), float(p.max()),
            _collapsed(p), ece, brier,
            float((p >= 0.55).mean()), float((p >= 0.60).mean()),
        )
        rows.append(row)

    for r in rows:
        fmt = f"{r[0]:<8}{r[1]:<5}{r[2]:<26}"
        for v in r[3:]:
            fmt += f"{v:>8.4f}" if isinstance(v, float) and not np.isnan(v) else f"{'—':>8}"
        print(fmt)

    print("\nlegend: in[.5,.6] = доля прогнозов в схлопнутом диапазоне 0.50-0.60; "
          "cov55/60 = coverage p>=0.55/0.60; ECE порог 0.05")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
