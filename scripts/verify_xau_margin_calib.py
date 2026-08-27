"""Post-fix verification of the retrained XAUUSD production model.

Checks:
  1. metadata.model_hash == recomputed fingerprint (self-hash integrity)
  2. Calibration input space is the logit margin (sigmoid a_ ~ -1, not ~ -3)
  3. OOS probability spread (std_p / p5-p95) and ECE / coverage on the
     production data path (train_mt5.build_full_df + build_training_matrix),
     using the same purged-split logic as production calibration.

Usage:
    python -m scripts.verify_xau_margin_calib
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np

from config.loader import load_config, resolve_asset_timeframe
from model.calibration import compute_ece
from scripts.train_mt5 import build_full_df, build_training_matrix
from scripts.verify_model_fingerprints import compute_model_fingerprint, verify_file


def main() -> int:
    cfg = load_config()
    asset = "XAUUSD"
    acfg = cfg["assets"][asset]
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    timeframe = resolve_asset_timeframe(cfg, asset)
    model_path = Path(acfg["model_path"])
    if not model_path.exists():
        print(f"FAIL: model not found: {model_path}")
        return 1

    print(f"=== XAUUSD post-fix verification ({timeframe}) ===")
    print(f"model: {model_path}")

    # 1. fingerprint integrity
    row = verify_file(str(model_path))
    print(f"fingerprint verdict : {row['verdict']}  ({row.get('note', '')})")
    print(f"  stored            : {row.get('stored_model_hash')}")
    print(f"  recomputed        : {row.get('recomputed_fingerprint')}")
    if row["verdict"] not in ("NEW-OK",):
        print("FAIL: self-hash does not verify")
        return 1

    bundle = joblib.load(model_path)
    model = bundle["model"]
    cc = model.calibrated_classifiers_[0]
    cal = cc.calibrators[0]
    a_ = float(getattr(cal, "a_", float("nan")))
    b_ = float(getattr(cal, "b_", float("nan")))
    print(f"calibrator input    : sigmoid a_={a_:.4f} b_={b_:.4f} (logit-margin => a_ ~ -1; raw-prob => a_ ~ -3)")

    # 2. OOS tail evaluation on production features
    print("building features (production path) ...", flush=True)
    raw = None
    from scripts.retrain_with_real_trades import read_candles

    raw = read_candles(db_path, timeframe, asset)
    if raw is None or raw.empty:
        print("FAIL: no candles")
        return 1
    full = build_full_df(raw, cfg, db_path, asset, timeframe)
    X_all, y_all, _avail = build_training_matrix(full, cfg=cfg)
    print(f"rows={len(X_all)}")

    # use the same purge-split as production calibration (positional slicing)
    gap = int(cfg.get("model", {}).get("purge_gap_bars", 36))
    n_cal = int(len(X_all) * 0.2)
    X = X_all.to_numpy()
    y = y_all.to_numpy()
    end = len(X_all)
    cal_start = max(0, end - n_cal - gap)
    cal_stop = max(0, end - gap)
    oos_start = max(0, end - max(gap * 2, 1000))  # wider OOS tail for meaningful coverage
    y_cal, y_oos = y[cal_start:cal_stop], y[oos_start:end]

    p_cal = model.predict_proba(X[cal_start:cal_stop])[:, 1]
    p_oos = model.predict_proba(X[oos_start:end])[:, 1]
    ece_cal, _ = compute_ece(y_cal, p_cal)
    ece_oos, _ = compute_ece(y_oos, p_oos)
    cov_055 = float((p_oos >= 0.55).mean() * 100.0)
    cov_060 = float((p_oos >= 0.60).mean() * 100.0)

    print(f"OOS  n={len(p_oos)}  p_mean={p_oos.mean():.4f}  std_p={p_oos.std():.4f}  "
          f"p5={np.percentile(p_oos, 5):.4f}  p95={np.percentile(p_oos, 95):.4f}  "
          f"min={p_oos.min():.4f}  max={p_oos.max():.4f}")
    print(f"OOS  ECE={ece_oos:.4f}  (cal-slice ECE={ece_cal:.4f})")
    print(f"OOS  coverage: p>=0.55 {cov_055:.1f}%   p>=0.60 {cov_060:.1f}%")

    ok = True
    if not (0.005 <= p_oos.std() <= 0.12):
        print(f"WARN: std_p={p_oos.std():.4f} outside expected 0.005-0.12")
    if ece_oos > 0.05:
        print(f"WARN: OOS ECE {ece_oos:.4f} > 0.05")
    print("RESULT:", "OK" if ok else "CHECK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
