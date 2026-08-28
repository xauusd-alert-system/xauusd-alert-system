"""
Deploy guard for the nightly retrain pipeline (Step 7 / Phase 6 / audit #25).

The nightly retrain (scripts/overnight.py stages 4-5) must NEVER silently
overwrite a good production model with a bad one. This module provides the
"does the freshly retrained model deserve to replace the incumbent?" check:

    --backup   copy each enabled asset's current production model to a
               sidecar backup (model_path + .deploy_guard.bak) BEFORE the
               nightly retrain overwrites it. Idempotent across nights.
    --check    after the nightly retrain, walk-forward validate the newly
               retrained model family against the incumbent on the SAME
               freshly backfilled out-of-sample windows. If the new model is
               no better than (or regressed beyond) the incumbent, restore the
               backed-up model and return exit code 1 so scripts/overnight.py
               reports the stage FAILED -> Telegram ❌ instead of a silent
               green tick.
    --status   print the current deploy-guard configuration.

No-look-ahead contract (#25, matching the repo-wide rule):
    * The incumbent (deployed) model is a STATIC file: it is only ever scored
      on test windows it has NOT been (re)trained on during this night.
    * The candidate is evaluated per walk-forward fold by training a FRESH
      model on that fold's train window ONLY and scoring the immediately
      following out-of-sample test window - exactly the honest evaluation
      `scripts/run_backtest.py` already uses (HIGH 11: fold models live in
      temp files and never touch the production path).
    * Both sides are scored on IDENTICAL test windows, so the comparison is
      fair: candidate's OOS metric vs incumbent's OOS metric on the same data.

Decision rule (conservative - never overwrite a good model without evidence):
    1. Select the primary metric (default `expectancy`); walk the fallback
       chain (`sharpe_ratio` -> `win_rate` -> `total_pnl`) until a metric both
       sides carry a present, non-NaN value for.
    2. If the candidate has fewer than `min_trades` OOS trades while the
       incumbent has enough -> REJECT (refuse to deploy on thin evidence).
    3. If the incumbent is thin but the candidate has real evidence -> DEPLOY.
    4. Otherwise deploy iff candidate_value >= incumbent_value - tolerance
       (tolerance >= 0 allows a small, configurable regression).

All knobs live in `config/config.yaml` under `deploy_guard:`.
"""
import argparse
import copy
import logging
import os
import shutil
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.metrics import compute_metrics, trades_to_dataframe
from backtest.walk_forward import generate_windows
from config.loader import load_config
from data.storage import read_candles
from model.ensemble_backtest import EnsembleBacktester
from model.predictor import ModelPredictor
from model.registry import ModelRegistry, file_sha256
from model.trainer import (
    build_training_matrix,
    calibrate_model,
    save_model,
    train_model,
)
from scripts.train_mt5 import build_full_df

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("deploy_guard")


class RegistryPreflightError(RuntimeError):
    """Raised when the deploy candidate is unregistered or hash-corrupted
    (Model Registry pre-flight, ТЗ 8.4). Blocking the deploy is the point."""

    def __init__(self, reason: str, registry_id: str | None = None):
        super().__init__(reason)
        self.registry_id = registry_id

# Minimum out-of-sample trades required before a side's primary metric is
# trusted (configurable via deploy_guard.min_trades).
DEFAULT_MIN_TRADES = 20

# Fallback metric chain used when the primary metric is missing/degenerate on
# either side. Same keys as backtest.metrics.compute_metrics() output.
DEFAULT_FALLBACK_CHAIN = ("sharpe_ratio", "win_rate", "total_pnl")

# Minimum training rows per candidate fold before we treat the fold as fit
# (mirrors scripts/run_backtest.py's "len(X_train) >= 30 and y_train.nunique() >= 2").
_MIN_TRAIN_ROWS = 30


# --------------------------------------------------------------------------- #
# Pure decision helpers (fully unit-testable with plain dicts)
# --------------------------------------------------------------------------- #
def _num(value):
    """Return a float if value is a present finite number, else None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _out(deploy, metric, d_val, c_val, reason, tolerance, min_trades):
    return {
        "deploy": deploy,
        "metric": metric,
        "deployed_value": d_val,
        "candidate_value": c_val,
        "reason": reason,
        "tolerance": float(tolerance),
        "min_trades": int(min_trades),
    }


def is_improvement(
    deployed: dict,
    candidate: dict,
    primary: str = "expectancy",
    tolerance: float = 0.0,
    min_trades: int = DEFAULT_MIN_TRADES,
    fallback_chain=DEFAULT_FALLBACK_CHAIN,
) -> dict:
    """Decide whether the candidate model may replace the incumbent.

    Both inputs are aggregate metrics dicts as produced by
    `aggregate_fold_metrics` (n_trades, expectancy, ..., total_pnl). Returns a
    dict with `deploy` (bool), `metric`, `deployed_value`, `candidate_value`,
    `reason`, `tolerance`, `min_trades`.
    """
    chain = []
    for k in [primary] + list(fallback_chain):
        if k not in chain:
            chain.append(k)

    metric = None
    d_val = None
    c_val = None
    for k in chain:
        dv = _num((deployed or {}).get(k))
        cv = _num((candidate or {}).get(k))
        if dv is not None and cv is not None:
            metric, d_val, c_val = k, dv, cv
            break
    if metric is None:
        metric = "total_pnl"
        d_val = _num((deployed or {}).get("total_pnl"))
        c_val = _num((candidate or {}).get("total_pnl"))

    cand_trades = int((candidate or {}).get("n_trades") or 0)
    dep_trades = int((deployed or {}).get("n_trades") or 0)
    thin_cand = cand_trades is not None and cand_trades < min_trades
    thin_dep = dep_trades is not None and dep_trades < min_trades

    # 2. Never replace a proven model with one that has too little OOS evidence.
    if thin_cand and not thin_dep:
        return _out(False, metric, d_val, c_val,
                    "candidate_too_little_evidence", tolerance, min_trades)
    # 3. A candidate with real evidence may replace a model with no evidence.
    if not thin_cand and thin_dep:
        return _out(True, metric, d_val, c_val,
                    "candidate_has_evidence_incumbent_thin", tolerance, min_trades)

    # 4. Both sides adequate (or both thin): compare with tolerance.
    if c_val is not None and d_val is not None and c_val >= d_val - tolerance:
        reason = "within_tolerance" if c_val < d_val else "ok"
        return _out(True, metric, d_val, c_val, reason, tolerance, min_trades)
    return _out(False, metric, d_val, c_val,
                "regressed_beyond_tolerance", tolerance, min_trades)


def aggregate_fold_metrics(fold_metrics: list) -> dict:
    """Aggregate per-fold OOS metrics into one comparable summary.

    Scoring metrics are averaged across folds; `total_pnl` is summed across
    folds; `max_drawdown` is the worst (most negative); `n_trades` is the sum.
    A side with zero folds yields all-metric None + n_trades=0.
    """
    if not fold_metrics:
        return {"n_folds": 0, "n_trades": 0, "expectancy": None, "win_rate": None,
                "profit_factor": None, "sharpe_ratio": None, "sortino_ratio": None,
                "total_pnl": None, "max_drawdown": None}
    out = {"n_folds": len(fold_metrics),
           "n_trades": int(sum(int(m.get("n_trades", 0) or 0) for m in fold_metrics))}
    for k in ("expectancy", "win_rate", "profit_factor", "sharpe_ratio", "sortino_ratio"):
        vals = [v for v in (_num(m.get(k)) for m in fold_metrics) if v is not None]
        out[k] = float(np.mean(vals)) if vals else None
    pnls = [v for v in (_num(m.get("total_pnl")) for m in fold_metrics) if v is not None]
    out["total_pnl"] = float(sum(pnls)) if pnls else None
    dds = [v for v in (_num(m.get("max_drawdown")) for m in fold_metrics) if v is not None]
    out["max_drawdown"] = float(min(dds)) if dds else None
    return out


# --------------------------------------------------------------------------- #
# Config / filesystem helpers
# --------------------------------------------------------------------------- #
def _merge_asset_section(cfg: dict, asset_key: str, section: str) -> dict:
    """Return a deep copy of cfg with the asset's `section` merged over the
    global one (identical contract to scripts/run_backtest.py::merge_asset_cfg)."""
    base = cfg.get(section, {})
    asset_section = cfg.get("assets", {}).get(asset_key, {}).get(section)
    merged = copy.deepcopy(base)
    if asset_section:
        merged.update(asset_section)
    cfg_copy = copy.deepcopy(cfg)
    cfg_copy[section] = merged
    return cfg_copy


def enabled_assets(cfg: dict) -> list:
    return [k for k, v in cfg.get("assets", {}).items() if v.get("enabled", False)]


def check_config_sync(cfg: dict) -> list[str]:
    """Validate that execution.enabled_assets ⊆ assets.*.enabled=true.

    A phantom asset in execution.enabled_assets silently produces trade
    proposals whose signals resolve to stale or missing models (the
    XAGUSD/GBPUSD desync case found 2026-08-25).

    Returns a list of violation strings (empty = OK).
    """
    errors: list[str] = []
    exec_assets = set(cfg.get("execution", {}).get("enabled_assets", []))
    assets_enabled = enabled_assets(cfg)
    assets_enabled_set = set(assets_enabled)
    # 1. execution mentions an asset whose model/config is disabled
    phantom = exec_assets - assets_enabled_set
    if phantom:
        errors.append(
            f"execution.enabled_assets contains disabled asset(s): {sorted(phantom)}"
            f" (assets.*.enabled=false for these)."
        )
    # 2. assets.*.enabled=true but NOT in execution.enabled_assets
    #    (model is trained but never traded — usually intentional, but
    #    flag as info so the owner can confirm it's deliberate)
    missing = assets_enabled_set - exec_assets
    if missing:
        print(f"  CONFIG INFO: assets.*.enabled=true but not in execution: {sorted(missing)}")
    return errors


def backup_production_models(cfg: dict, backup_suffix: str = ".deploy_guard.bak") -> list:
    """Copy each enabled asset's current model to `<model_path><backup_suffix>`.

    Idempotent: an existing backup is never overwritten (a previous partial run
    must not silently replace a good backup). Missing model files are noted and
    skipped (there is nothing to protect yet). Returns list of
    (asset, status, ok) tuples.
    """
    results = []
    for asset in enabled_assets(cfg):
        mp = cfg["assets"][asset].get("model_path")
        if not mp or not os.path.exists(mp):
            results.append((asset, "no_model", True))
            continue
        bak = mp + backup_suffix
        if os.path.exists(bak):
            results.append((asset, "already_backed_up", True))
            continue
        os.makedirs(os.path.dirname(mp) or ".", exist_ok=True)
        shutil.copy2(mp, bak)
        results.append((asset, "backed_up", True))
        logger.info("[%s] backed up %s -> %s", asset, mp, bak)
    return results


# --------------------------------------------------------------------------- #
# Walk-forward evaluation (both sides)
# --------------------------------------------------------------------------- #
def _apply_predictor_to_df(predictor, test_df: pd.DataFrame):
    """Return (p_long, p_short) arrays for test_df, or (None, None) on failure
    (mirrors scripts/run_backtest.py neutral fallback: 0.5/0.5)."""
    try:
        preds = predictor.predict_proba(test_df.fillna(0.0))
        return preds["p_long"].values, preds["p_short"].values
    except Exception:  # noqa: BLE001 - a failing predictor must not crash the guard
        return None, None


def _candidate_fold_predictor(train_df: pd.DataFrame, cfg: dict, asset_key: str):
    """Train a fresh candidate model on `train_df` ONLY (no look-ahead) and
    return a ModelPredictor, or None if the fold cannot be trained.

    The model is saved to a temp file (never the production path - HIGH 11)
    and loaded into memory; the temp file is removed immediately after.
    """
    cfg_inner = _merge_asset_section(cfg, asset_key, "labeling")
    try:
        X, y, cols = build_training_matrix(train_df, cfg=cfg_inner)
        if len(X) < _MIN_TRAIN_ROWS or y.nunique() < 2:
            return None
        base = train_model(X, y, cfg_inner)
        calibrated = calibrate_model(base, X, y, cfg_inner)
        fd, tmp = tempfile.mkstemp(prefix="deploy_guard_", suffix=".joblib")
        os.close(fd)
        try:
            save_model(calibrated, cols, tmp)
            return ModelPredictor(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] candidate fold training failed (%s); using neutral.", asset_key, e)
        return None


def _score_window(test_df: pd.DataFrame, cfg: dict, asset_key: str, predictor) -> dict:
    """Score one out-of-sample test window through the full ensemble backtest."""
    test = test_df.copy()
    pl, ps = _apply_predictor_to_df(predictor, test)
    if pl is None or ps is None:
        test["ml_p_long"] = 0.5
        test["ml_p_short"] = 0.5
    else:
        test["ml_p_long"] = pl
        test["ml_p_short"] = ps
    cfg_inner = _merge_asset_section(cfg, asset_key, "ensemble")
    cfg_inner = _merge_asset_section(cfg_inner, asset_key, "labeling")
    engine = EnsembleBacktester(cfg_inner, asset_key=asset_key)
    trades = engine.run(test.reset_index(drop=True))
    return compute_metrics(trades_to_dataframe(trades))


def _slice(df: pd.DataFrame, start_ts: int, end_ts: int) -> pd.DataFrame:
    return df[(df["timestamp_utc"] >= start_ts) & (df["timestamp_utc"] < end_ts)]


def evaluate_incumbent(cfg: dict, asset_key: str, deployed_path: str, df: pd.DataFrame,
                       windows: list) -> dict:
    """Static incumbent model scored on every OOS window (never retrained)."""
    if not os.path.exists(deployed_path):
        return None
    try:
        predictor = ModelPredictor(deployed_path)
    except Exception:  # noqa: BLE001
        predictor = None
    folds = []
    for w in windows:
        test_df = _slice(df, w.test_start_ts, w.test_end_ts)
        if len(test_df) == 0:
            continue
        folds.append(_score_window(test_df, cfg, asset_key, predictor))
    return aggregate_fold_metrics(folds)


def evaluate_candidate(cfg: dict, asset_key: str, df: pd.DataFrame, windows: list) -> dict:
    """Fresh model trained per fold (train window only) scored OOS per fold."""
    folds = []
    for w in windows:
        train_df = _slice(df, w.train_start_ts, w.train_end_ts)
        test_df = _slice(df, w.test_start_ts, w.test_end_ts)
        if len(test_df) == 0:
            continue
        predictor = _candidate_fold_predictor(train_df, cfg, asset_key)
        folds.append(_score_window(test_df, cfg, asset_key, predictor))
    return aggregate_fold_metrics(folds)


def decide_from_evaluations(
    deployed_metrics: dict,
    candidate_metrics: dict,
    cfg: dict,
    has_incumbent: bool,
    windows_valid: bool,
) -> dict:
    """Pure decision given already-computed aggregate metrics (either may be
    None), plus environment facts. Factor of `guard_asset` made testable."""
    dg = cfg.get("deploy_guard", {})
    if not windows_valid:
        # Cannot validate honestly. Conservative: never overwrite a good model
        # on no evidence; deploy a first model when there is nothing to protect.
        return {"deploy": not has_incumbent, "metric": None,
                "deployed_value": None, "candidate_value": None,
                "reason": "no_valid_windows", "tolerance": float(dg.get("tolerance", 0.0)),
                "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}
    if not has_incumbent or deployed_metrics is None:
        return {"deploy": True, "metric": None,
                "deployed_value": None, "candidate_value": None,
                "reason": "no_deployed_model", "tolerance": float(dg.get("tolerance", 0.0)),
                "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}
    if candidate_metrics is None:
        return {"deploy": False, "metric": None,
                "deployed_value": None, "candidate_value": None,
                "reason": "candidate_evaluation_failed",
                "tolerance": float(dg.get("tolerance", 0.0)),
                "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}
    return is_improvement(
        deployed_metrics,
        candidate_metrics,
        primary=str(dg.get("primary_metric", "expectancy")),
        tolerance=float(dg.get("tolerance", 0.0)),
        min_trades=int(dg.get("min_trades", DEFAULT_MIN_TRADES)),
        fallback_chain=tuple(dg.get("fallback_metrics", DEFAULT_FALLBACK_CHAIN)),
    )


def guard_asset(cfg: dict, asset_key: str, deployed_path: str) -> dict:
    """Full per-asset guard: load fresh history, walk-forward validate both
    sides on identical windows, and decide whether to deploy the candidate.

    `deployed_path` is the backed-up incumbent model file.
    """
    dg = cfg.get("deploy_guard", {})
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    timeframe = asset_cfg.get("timeframe") or cfg.get("market_data", {}).get("timeframe", "M5")
    wf = cfg.get("backtest", {}).get("walk_forward", {})
    train_days = int(wf.get("train_window_days", 300))
    test_days = int(wf.get("test_window_days", 50))
    step_days = int(wf.get("step_days", 50))

    raw = read_candles(db_path, timeframe, asset_key)
    if raw.empty:
        return {"asset": asset_key, "deploy": False, "metric": None,
                "deployed_value": None, "candidate_value": None,
                "reason": "no_candles", "tolerance": float(dg.get("tolerance", 0.0)),
                "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}

    df = build_full_df(raw, cfg, db_path=db_path, asset_key=asset_key, timeframe=timeframe)
    df["timestamp_utc"] = df["timestamp_utc"].astype("int64")
    windows = generate_windows(df, train_days, test_days, step_days)
    if not windows:
        return {"deploy": not os.path.exists(deployed_path), "metric": None,
                "deployed_value": None, "candidate_value": None,
                "reason": "no_valid_windows", "tolerance": float(dg.get("tolerance", 0.0)),
                "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}

    deployed_m = evaluate_incumbent(cfg, asset_key, deployed_path, df, windows)
    candidate_m = evaluate_candidate(cfg, asset_key, df, windows)
    dec = decide_from_evaluations(
        deployed_m, candidate_m, cfg,
        has_incumbent=os.path.exists(deployed_path), windows_valid=True,
    )
    dec["asset"] = asset_key
    return dec


# --------------------------------------------------------------------------- #
# Model Registry pre-flight (ТЗ 8.4): never deploy an unregistered or
# corrupted model. Deploying is a registry-visible event, so the candidate
# must be cataloged and its file hash must still match the registered one.
# --------------------------------------------------------------------------- #
def registry_preflight_check(cfg: dict, asset_key: str, model_path: str,
                             registry: ModelRegistry | None = None) -> dict:
    """Verify the deployable model is registered AND its file hash matches.

    Returns {"ok": bool, "reason": str, "registry_id": str|None}.
    Fail-closed: any mismatch blocks the deploy with a clear reason.

    Matching strategy: prefer an exact registered model_path (the deploy
    candidate path is stable across retrains), fall back to a hash match
    (restored/renamed artifacts). Once matched, the CURRENT file content is
    compared against the registered hash - so a file corrupted AFTER
    registration is reported as model_corrupted, not unregistered.
    """
    if not os.path.exists(model_path):
        return {"ok": True, "reason": "no_model_file", "registry_id": None}
    reg = registry if registry is not None else ModelRegistry()
    try:
        sha = file_sha256(model_path)
    except OSError as e:
        return {"ok": False, "reason": f"hash_failed:{e}", "registry_id": None}

    entries = reg.list_entries(asset=asset_key)
    norm = os.path.normcase(os.path.abspath(model_path))
    match = next(
        (e for e in entries
         if os.path.normcase(os.path.abspath(e.model_path)) == norm),
        None,
    )
    if match is None:
        match = next((e for e in entries if e.file_sha256 == sha), None)
    if match is None:
        return {"ok": False,
                "reason": "model_not_registered: deploy candidate "
                          f"{model_path} (sha256 {sha[:12]}...) has no "
                          "registry entry; train via scripts/train_all_assets "
                          "or register it explicitly",
                "registry_id": None}
    if not reg.verify(match.registry_id):
        return {"ok": False,
                "reason": f"model_corrupted: deploy candidate {model_path} "
                          f"no longer matches registered hash of "
                          f"{match.registry_id}",
                "registry_id": match.registry_id}
    return {"ok": True, "reason": "registered_and_verified",
            "registry_id": match.registry_id}


def validate_and_deploy(cfg: dict, backup_suffix: str = ".deploy_guard.bak",
                        registry: "ModelRegistry | None" = None):
    """Compare each asset's newly retrained model vs its nightly backup.

    On REJECT (or evaluation error), restore the backed-up (good) model over
    the production path and record `rolled_back=True`; the caller maps any
    rejected/errored asset to exit code 1 (=> overnight stage FAILED => ❌).
    The backup sidecar is cleaned up in all terminal states.
    Returns (decisions, failed: bool).
    """
    dg = cfg.get("deploy_guard", {})
    decisions = []
    failed = False
    for asset in enabled_assets(cfg):
        mp = cfg["assets"][asset].get("model_path")
        bak = (mp + backup_suffix) if mp else None
        if not mp or not bak or not os.path.exists(bak):
            # Nothing backed up -> nothing to roll back to; keep the new model.
            decisions.append({
                "asset": asset, "deploy": True, "metric": None,
                "deployed_value": None, "candidate_value": None,
                "reason": "no_backup_no_rollback",
                "tolerance": float(dg.get("tolerance", 0.0)),
                "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES)),
            })
            continue
        rolled_back = False
        try:
            # Registry pre-flight (ТЗ 8.4): the newly retrained candidate must
            # be registered and hash-verified BEFORE any deploy decision.
            pre = registry_preflight_check(cfg, asset, mp, registry=registry)
            if not pre["ok"]:
                raise RegistryPreflightError(pre["reason"], pre.get("registry_id"))
            dec = guard_asset(cfg, asset, bak)
            dec["registry_id"] = pre["registry_id"]
            if not dec.get("deploy", True):
                failed = True
                if os.path.exists(bak):
                    shutil.copy2(bak, mp)
                    rolled_back = True
                    logger.warning(
                        "[%s] model rejected (%s); restored %s from backup.",
                        asset, dec.get("reason"), mp,
                    )
            else:
                logger.info("[%s] deploy OK (%s); keeping new model.", asset, dec.get("reason"))
        except RegistryPreflightError as e:
            # Registry pre-flight (ТЗ 8.4): unregistered or corrupted deploy
            # candidate -> blocked with a verbatim, actionable reason.
            failed = True
            if os.path.exists(bak):
                shutil.copy2(bak, mp)
                rolled_back = True
                logger.warning(
                    "[%s] deploy BLOCKED by registry pre-flight (%s); restored %s from backup.",
                    asset, e, mp,
                )
            else:
                logger.warning(
                    "[%s] deploy BLOCKED by registry pre-flight (%s); no backup to restore.",
                    asset, e,
                )
            dec = {"asset": asset, "deploy": False, "metric": None,
                   "deployed_value": None, "candidate_value": None,
                   "reason": str(e), "registry_id": e.registry_id,
                   "tolerance": float(dg.get("tolerance", 0.0)),
                   "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}
        except Exception as e:  # noqa: BLE001 - an error must not deploy blindly
            logger.warning("[%s] deploy guard errored (%s); rolling back.", asset, e)
            failed = True
            dec = {"asset": asset, "deploy": False, "metric": None,
                   "deployed_value": None, "candidate_value": None,
                   "reason": f"error:{e}",
                   "tolerance": float(dg.get("tolerance", 0.0)),
                   "min_trades": int(dg.get("min_trades", DEFAULT_MIN_TRADES))}
            if os.path.exists(bak):
                shutil.copy2(bak, mp)
                rolled_back = True
                logger.warning("[%s] errored; restored %s from backup.", asset, mp)
        finally:
            if os.path.exists(bak):
                os.remove(bak)
        if rolled_back:
            dec["rolled_back"] = True
        dec["candidate_path"] = mp
        decisions.append(dec)
    return decisions, failed


def print_decisions(decisions: list) -> None:
    for dec in decisions:
        mark = "KEEP" if dec.get("deploy", True) else "ROLLED-BACK"
        print(
            f"  [{dec['asset']}] {mark}: metric={dec.get('metric')} "
            f"deployed={dec.get('deployed_value')} candidate={dec.get('candidate_value')} "
            f"reason={dec.get('reason')}"
        )
        if dec.get("rolled_back"):
            print(f"      restored {dec.get('candidate_path')} from nightly backup")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Nightly model deploy guard (#25).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup", action="store_true",
                       help="Back up current production models before nightly retrain.")
    group.add_argument("--check", action="store_true",
                       help="Validate newly retrained models and roll back regressions.")
    group.add_argument("--status", action="store_true",
                       help="Print the deploy-guard configuration.")
    args = parser.parse_args(argv)

    cfg = load_config()
    dg = cfg.get("deploy_guard", {})
    if not dg.get("enabled", True):
        print("Deploy guard disabled (deploy_guard.enabled=false); skipping.")
        return 0
    backup_suffix = str(dg.get("backup_suffix", ".deploy_guard.bak"))

    if args.status:
        print(f"enabled: {dg.get('enabled', True)}")
        print(f"primary_metric: {dg.get('primary_metric', 'expectancy')}")
        print(f"fallback_metrics: {dg.get('fallback_metrics', list(DEFAULT_FALLBACK_CHAIN))}")
        print(f"min_trades: {dg.get('min_trades', DEFAULT_MIN_TRADES)}")
        print(f"tolerance: {dg.get('tolerance', 0.0)}")
        print(f"backup_suffix: {backup_suffix}")
        return 0

    if args.backup:
        results = backup_production_models(cfg, backup_suffix)
        for asset, status, ok in results:
            print(f"  [{asset}] {status}")
        return 0

    # --check
    # Pre-flight: configuration consistency (2026-08-27)
    # execution.enabled_assets must be a strict subset of assets.*.enabled;
    # a phantom asset in the execution list silently produces trade proposals
    # whose signals resolve to stale/missing models (the XAGUSD/GBPUSD case).
    cfg_errors = check_config_sync(cfg)
    if cfg_errors:
        for e in cfg_errors:
            print(f"  CONFIG ERROR: {e}")
        print("\nDEPLOY GUARD: config sync failed -> exit 1")
        return 1

    # Pre-flight: weekend session-tag audit (2026-08-27)
    # FX trades at Sunday 21:00-24:00 UTC must not carry 'weekend' tag.
    from scripts.audit_weekend_tags import audit_weekend_tags
    weekend_violations = audit_weekend_tags()
    if weekend_violations:
        print(f"  WEEKEND TAG ERROR: {len(weekend_violations)} FX trade(s) tagged 'weekend' at Sunday 21-24 UTC")
        print("\nDEPLOY GUARD: weekend tag audit failed -> exit 1")
        return 1

    decisions, failed = validate_and_deploy(cfg, backup_suffix)
    print(f"Deploy guard --check over {len(decisions)} enabled asset(s):")
    print_decisions(decisions)
    if failed:
        print("\nDEPLOY GUARD: night rejected (at least one asset rolled back) -> exit 1")
        return 1
    print("\nDEPLOY GUARD: all assets OK -> exit 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
