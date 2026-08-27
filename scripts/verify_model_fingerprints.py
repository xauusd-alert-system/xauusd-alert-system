"""Audit every model bundle under output/models against its stored self-hash
AND against probability degeneracy.

For each ``*.joblib`` file (recursive, so backups/candidates/retrain dirs are
covered too):

  * NEW bundle (dict with ``model`` + ``feature_cols`` [+ ``metadata``]):
      - ``metadata.model_hash`` present -> recompute the deterministic content
        fingerprint (``compute_model_fingerprint``) and compare. Verdict
        ``NEW-OK`` or ``NEW-MISMATCH``.
      - ``metadata.model_hash`` missing/None -> the pipeline treats it via the
        legacy file-sha256 path; the recomputed fingerprint is shown for
        reference. Verdict ``LEGACY``.
  * anything else (raw xgboost / old format / unreadable) -> ``UNRECOGNIZED``
    with the file sha256, so nothing is silently skipped.

Degeneracy audit (2026-08-27, GBPUSD artifact):
  When real recent history is available for the asset (config + DB), the model
  is scored on the trailing ``--probe-bars`` bars and the standard deviation of
  ``p_long`` / ``p_short`` is measured. Verdict ``DEGENERATE`` is emitted when:

    * ``std_p_long < --min-std`` OR ``std_p_short < --min-std`` (default 0.01)
      — probabilities collapsed to a narrow band, the model cannot separate
      the two sides (GBPUSD pre-fix: constant 0.0002; XAGUSD: constant
      0.475371 on 100% of bars), or
    * the output is truly constant (``nunique <= 1``) even if the std check is
      bypassed.

  A ``DEGENERATE`` / ``NEW-MISMATCH`` / ``UNRECOGNIZED`` verdict on a
  PRODUCTION model (a path listed in ``config.assets.*.model_path``) makes the
  process exit 1, so the overnight pipeline fails-closed and cannot silently
  keep a broken model deployed.

Read-only. Writes logs/model_fingerprint_audit.csv.
"""
import argparse
import hashlib
import os
import sys

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from model.trainer import compute_model_fingerprint

MODEL_DIR = os.path.join("output", "models")
OUT_CSV = os.path.join("logs", "model_fingerprint_audit.csv")

_KNOWN_ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD", "ETHUSD")


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_from_name(path: str) -> str:
    base = os.path.basename(path)
    for a in _KNOWN_ASSETS:
        if base.lower().startswith(a.lower()):
            return a.upper()
    return "?"


def _prob_columns(proba: np.ndarray, classes) -> dict:
    """Map predict_proba columns to named probabilities by class VALUE.

    Mirrors model.predictor.ModelPredictor: 3-class encoding is
    {0: short, 1: no_trade, 2: long}, binary is {0: short, 1: long}.
    """
    order = {int(c): i for i, c in enumerate(classes)}
    if 2 in order:
        return {
            "p_short": proba[:, order[0]],
            "p_no_trade": proba[:, order[1]],
            "p_long": proba[:, order[2]],
        }
    return {
        "p_short": proba[:, order[0]],
        "p_long": proba[:, order[1]],
    }


def degeneracy_stats(model, feature_cols: list, probe_X: pd.DataFrame) -> dict | None:
    """Score a loaded model on real feature rows and return spread statistics.

    Returns None when the model cannot be scored (no classes_, predict error).
    """
    classes = getattr(model, "classes_", None)
    if classes is None:
        return None
    try:
        X = probe_X[list(feature_cols)].astype(float)
        proba = model.predict_proba(X)
    except Exception as exc:
        return {"error": f"{exc!r}"}
    cols = _prob_columns(proba, classes)
    stats = {"n": int(len(proba))}
    for name, values in cols.items():
        stats[f"std_{name}"] = float(np.std(values))
        stats[f"nunique_{name}"] = int(pd.Series(values).nunique())
    return stats


def _is_degenerate(stats: dict | None, min_std: float) -> tuple[bool, str]:
    if not stats or "error" in stats or "n" not in stats or stats["n"] == 0:
        return False, "no probe data"
    stds = [v for k, v in stats.items() if k.startswith("std_")]
    nuniques = [v for k, v in stats.items() if k.startswith("nunique_")]
    if nuniques and min(nuniques) <= 1:
        return True, f"constant output (nunique={min(nuniques)})"
    if stds and min(stds) < min_std:
        return True, f"min std_p={min(stds):.6f} < {min_std}"
    return False, ""


def verify_file(path: str, probe_X: pd.DataFrame | None = None,
                min_std: float = 0.01, asset_key: str | None = None) -> dict:
    row = {
        "file": path,
        "asset": asset_key or _asset_from_name(path),
        "size_bytes": os.path.getsize(path),
        "file_sha256": _file_sha256(path),
        "trained_at_utc": None,
        "stored_model_hash": None,
        "recomputed_fingerprint": None,
        "verdict": None,
        "note": "",
        "probe_n": None,
        "std_p_long": None,
        "std_p_short": None,
        "nunique_p_long": None,
        "nunique_p_short": None,
        "degenerate": False,
    }
    try:
        bundle = joblib.load(path)
    except Exception as exc:
        row["verdict"] = "UNRECOGNIZED"
        row["note"] = f"load failed: {exc!r}"
        return row

    if not isinstance(bundle, dict) or "model" not in bundle:
        row["verdict"] = "UNRECOGNIZED"
        row["note"] = f"not a bundle dict (keys={list(bundle) if isinstance(bundle, dict) else type(bundle).__name__})"
        return row

    meta = bundle.get("metadata") or {}
    row["trained_at_utc"] = meta.get("trained_at_utc")
    stored = meta.get("model_hash")
    row["stored_model_hash"] = stored
    cols = bundle.get("feature_cols")

    # Degeneracy probe (real recent bars when available for the asset).
    if probe_X is not None and cols is not None:
        stats = degeneracy_stats(bundle["model"], cols, probe_X)
        if stats and "error" not in stats:
            row["probe_n"] = stats["n"]
            row["std_p_long"] = stats.get("std_p_long")
            row["std_p_short"] = stats.get("std_p_short")
            row["nunique_p_long"] = stats.get("nunique_p_long")
            row["nunique_p_short"] = stats.get("nunique_p_short")
            degen, why = _is_degenerate(stats, min_std)
            row["degenerate"] = degen
            if degen:
                row["note"] = f"DEGENERATE: {why}"

    if stored is None:
        row["verdict"] = "LEGACY" if not row["degenerate"] else "DEGENERATE"
        if not row["degenerate"]:
            row["note"] = "self-hash absent; pipeline verifies via file sha256"
        if cols is not None:
            try:
                row["recomputed_fingerprint"] = compute_model_fingerprint(bundle["model"], cols)
            except Exception as exc:
                row["note"] += f" | fingerprint failed: {exc!r}"
        return row

    try:
        fp = compute_model_fingerprint(bundle["model"], cols)
    except Exception as exc:
        row["verdict"] = "NEW-MISMATCH"
        row["note"] = f"fingerprint recompute failed: {exc!r}"
        return row
    row["recomputed_fingerprint"] = fp
    if fp != stored:
        row["verdict"] = "NEW-MISMATCH"
        row["note"] = "stored self-hash != recomputed fingerprint"
    elif row["degenerate"]:
        row["verdict"] = "DEGENERATE"
    else:
        row["verdict"] = "NEW-OK"
        row["note"] = "self-hash matches recomputed fingerprint"
    return row


def _build_probe_frame(cfg: dict, asset_key: str, timeframe: str,
                       db_path: str, probe_days: int, probe_bars: int) -> pd.DataFrame | None:
    """Build a real recent-features frame for one asset (cached by caller).

    Mirrors scripts/diag_calib_check: cap raw history to recent days, build the
    production feature set, and expand regime one-hots so the model scores the
    same columns it saw at training time.
    """
    try:
        from scripts.run_backtest import load_asset_history
        from scripts.train_mt5 import build_full_df
        raw = load_asset_history(db_path, timeframe, asset_key)
        if raw is None or len(raw) == 0:
            return None
        if probe_days:
            cutoff = raw["timestamp_utc"].max() - probe_days * 86400.0
            raw = raw[raw["timestamp_utc"] >= cutoff]
        df = build_full_df(raw, cfg, db_path=db_path, asset_key=asset_key,
                           timeframe=timeframe)
        if df is None or len(df) == 0:
            return None
        tail = df.tail(probe_bars).copy()
        # ModelPredictor expands missing regime_<label> one-hots from `regime`;
        # do the same so the completeness filter sees training-time columns.
        if "regime" in tail.columns:
            from regime.classifier import regime_onehot_df
            onehot = regime_onehot_df(tail)
            for c in onehot.columns:
                if c not in tail.columns:
                    tail[c] = onehot[c]
        return tail
    except Exception as exc:  # noqa: BLE001 - probe must never crash the audit
        print(f"  !! probe build failed for {asset_key}: {exc!r}")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-std", type=float, default=0.01,
                        help="std p_long/p_short below this flags DEGENERATE (default 0.01)")
    parser.add_argument("--probe-days", type=int, default=60,
                        help="recent raw-history window used to build the probe frame")
    parser.add_argument("--probe-bars", type=int, default=1500,
                        help="tail bars of the probe frame scored per model")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip the degeneracy probe entirely (fingerprints only)")
    args = parser.parse_args(argv)

    cfg = load_config()
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    production_paths = {
        os.path.normpath(v.get("model_path", ""))
        for v in cfg.get("assets", {}).values()
        if v.get("model_path")
    }

    files = sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(MODEL_DIR)
        for name in names
        if name.endswith(".joblib")
    )
    if not files:
        print(f"no *.joblib under {MODEL_DIR}")
        return 0

    # One probe frame per (asset, timeframe) — reused across backups/candidates.
    probe_cache: dict[tuple, pd.DataFrame] = {}
    rows = []
    for p in files:
        asset = _asset_from_name(p)
        probe = None
        if not args.no_probe and asset in cfg.get("assets", {}):
            tf = (cfg["assets"][asset].get("timeframe")
                  or cfg.get("market_data", {}).get("timeframe", "M5"))
            key = (asset, tf)
            if key not in probe_cache:
                print(f"building probe frame: {asset} {tf} ...", flush=True)
                probe_cache[key] = _build_probe_frame(
                    cfg, asset, tf, db_path, args.probe_days, args.probe_bars
                )
            probe = probe_cache[key]
        rows.append(verify_file(p, probe_X=probe, min_std=args.min_std, asset_key=asset))

    print(f"\n{'file':<58} {'asset':<7} {'verdict':<15} {'stored':<12} fp-match  std_p_long")
    print("-" * 130)
    for r in rows:
        fp = r["recomputed_fingerprint"]
        match = "n/a"
        if r["verdict"] == "NEW-OK":
            match = "YES"
        elif r["verdict"] in ("NEW-MISMATCH", "DEGENERATE"):
            match = "NO !!"
        stored = (r["stored_model_hash"] or "None")[:10]
        std_pl = f"{r['std_p_long']:.5f}" if r["std_p_long"] is not None else "-"
        print(f"{r['file']:<58} {r['asset']:<7} {r['verdict']:<15} {stored:<12} {match:<10} {std_pl}")
    print("-" * 130)

    from collections import Counter
    counts = Counter(r["verdict"] for r in rows)
    print("verdicts:", dict(counts))

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"wrote {OUT_CSV}")

    # Fail-closed: any broken PRODUCTION model fails the night.
    failed = [
        r["file"] for r in rows
        if r["verdict"] in ("DEGENERATE", "NEW-MISMATCH", "UNRECOGNIZED")
        and os.path.normpath(r["file"]) in production_paths
    ]
    if failed:
        print("\nFAIL: production models failed audit:")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("\nOK: no production model failed audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
