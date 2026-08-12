"""
Layer 0 (A1-A3) validation harness - run this LOCALLY, against the real MT5 database.

What it does, in order:

  Phase 1  Runs features/tests/test_no_lookahead.py twice: once on the pinned
           synthetic window (reproducible anywhere) and once on real candles
           read from your SQLite database, so the causality proofs are verified
           on the data the model is actually trained on.

  Phase 2  Rebuilds the feature frame on real history with the fixed code, and
           again with a verbatim reference implementation of the PRE-FIX
           formulas, then prints the before/after diff: per-feature change
           rates, the Asian-session breakdown, what dist_pdh_atr was really
           measured from, and the OBV level shift.

  Phase 3  Retrains the current model twice - once on legacy feature values,
           once on fixed ones - with identical config, split and seed, and
           prints AUC / accuracy / Brier / ECE / precision at the production
           gates, plus how much the model leaned on the five changed features.
           This is a MEASUREMENT, not a deployment: nothing is saved unless you
           pass --save-models.

The database is copied to a scratch file before reading, so the original is
never opened for writing (read_candles() calls init_schema()). Pass
--no-db-copy to read it in place.

Examples
--------
    python -m scripts.layer0_validation --db-path data/market_data_mt5.sqlite

    python -m scripts.layer0_validation \\
        --db-path data/market_data_mt5.sqlite --symbol XAUUSD --timeframe M15 \\
        --out output/layer0_validation.txt

Send me the whole output file.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

TEST_FILE = os.path.join("features", "tests", "test_no_lookahead.py")

# The five FEATURE_COLUMNS entries A1/A2 change. Everything else in the frame
# must come out bit-for-bit identical - the script checks that, it is not an
# assumption.
CHANGED_FEATURES = [
    "dist_asia_high_atr",
    "dist_asia_low_atr",
    "dist_pdh_atr",
    "dist_pdl_atr",
    "obv",
]


# ---------------------------------------------------------------------------
# output plumbing
# ---------------------------------------------------------------------------
class _Tee:
    def __init__(self, path):
        self._file = open(path, "w", encoding="utf-8")
        self._stdout = sys.stdout

    def write(self, text):
        self._stdout.write(text)
        self._file.write(text)

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        self._file.close()


def hr(title="", char="="):
    if title:
        print("\n" + char * 78)
        print(title)
        print(char * 78)
    else:
        print(char * 78)


def fmt_ts(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def table(headers, rows):
    """Fixed-width table so the pasted output stays readable."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(str(headers[i])), *(len(r[i]) for r in cells)) if cells
              else len(str(headers[i])) for i in range(len(headers))]
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(r[i].ljust(widths[i]) for i in range(len(headers))))


# ---------------------------------------------------------------------------
# Phase 1 - the no-look-ahead suite
# ---------------------------------------------------------------------------
def _load_test_module():
    """Load the test file by path so it works with or without features/tests/__init__.py."""
    path = os.path.join(REPO_ROOT, TEST_FILE)
    spec = importlib.util.spec_from_file_location("_layer0_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_embedded(label):
    """Minimal pytest-free runner: same functions, same asserts, same file."""
    module = _load_test_module()
    names = [n for n in vars(module) if n.startswith("test_")]
    names.sort(key=lambda n: vars(module)[n].__code__.co_firstlineno)

    passed = failed = skipped = 0
    for name in names:
        try:
            vars(module)[name]()
        except RuntimeError as exc:
            if str(exc).startswith("SKIP: "):
                print(f"  {name:<52} SKIPPED  ({str(exc)[6:]})")
                skipped += 1
                continue
            print(f"  {name:<52} FAILED")
            print("      " + "\n      ".join(traceback.format_exc().strip().splitlines()[-4:]))
            failed += 1
        except Exception:
            exc_type = sys.exc_info()[0].__name__
            if exc_type == "Skipped":  # pytest.skip raised outside pytest
                print(f"  {name:<52} SKIPPED")
                skipped += 1
                continue
            print(f"  {name:<52} FAILED")
            print("      " + "\n      ".join(traceback.format_exc().strip().splitlines()[-4:]))
            failed += 1
        else:
            print(f"  {name:<52} PASSED")
            passed += 1

    print(f"\n  [{label}] {passed} passed, {failed} failed, {skipped} skipped")
    return failed


def _run_pytest(label):
    cmd = [sys.executable, "-m", "pytest", TEST_FILE, "-v", "--tb=short",
           "-p", "no:cacheprovider", "-rs"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=dict(os.environ),
                          capture_output=True, text=True)
    out = proc.stdout.strip()
    print(out[-24000:] if out else "(no stdout)")
    if proc.stderr.strip():
        print("stderr:\n" + proc.stderr.strip()[-4000:])
    print(f"\n  [{label}] pytest exit code = {proc.returncode}")
    return proc.returncode


def phase1(args, db_for_tests):
    hr("PHASE 1  no-look-ahead suite (features/tests/test_no_lookahead.py)")

    try:
        import pytest  # noqa: F401
        runner, use_pytest = _run_pytest, True
        print("runner: pytest\n")
    except ImportError:
        runner, use_pytest = _run_embedded, False
        print("runner: built-in fallback (pytest is not installed)\n")

    results = {}

    print("-" * 78)
    print("1a. synthetic window (pinned end_ts, seed=42) - reproducible baseline")
    print("-" * 78)
    for key in ("XAU_TEST_DB", "XAU_TEST_SYMBOL", "XAU_TEST_BASE_TIMEFRAME"):
        os.environ.pop(key, None)
    results["synthetic"] = runner("synthetic")

    print()
    print("-" * 78)
    print(f"1b. REAL candles from {args.db_path} ({args.symbol})")
    print("-" * 78)
    os.environ["XAU_TEST_DB"] = db_for_tests
    os.environ["XAU_TEST_SYMBOL"] = args.symbol
    os.environ["XAU_TEST_BASE_TIMEFRAME"] = args.base_timeframe
    results["real"] = runner("real data")
    for key in ("XAU_TEST_DB", "XAU_TEST_SYMBOL", "XAU_TEST_BASE_TIMEFRAME"):
        os.environ.pop(key, None)

    return results


# ---------------------------------------------------------------------------
# Phase 2 - before/after feature diff on real history
# ---------------------------------------------------------------------------
def legacy_reference(out: pd.DataFrame) -> pd.DataFrame:
    """Recreate the PRE-FIX values of the five affected columns.

    Transcribed verbatim from features/indicators.py::build_all_indicators at
    master@7d5911f (the commit this branch forked from):

        atr_safe = out["atr"].replace(0, np.nan)
        day_group = out["timestamp_utc"] // 86400
        pdh = out["high"].groupby(day_group).transform("max").shift(288).ffill()
        pdl = out["low"].groupby(day_group).transform("min").shift(288).ffill()
        out["dist_pdh_atr"] = ((out["close"] - pdh) / atr_safe).fillna(0.0)
        out["dist_pdl_atr"] = ((out["close"] - pdl) / atr_safe).fillna(0.0)
        asia_mask = out["session"].str.contains("asia", na=False)
        asia_high = out["high"].where(asia_mask).groupby(day_group).transform("max").ffill()
        asia_low = out["low"].where(asia_mask).groupby(day_group).transform("min").ffill()
        out["dist_asia_high_atr"] = ((out["close"] - asia_high) / atr_safe).fillna(0.0)
        out["dist_asia_low_atr"] = ((out["close"] - asia_low) / atr_safe).fillna(0.0)
        out["obv"] = obv(out).fillna(0)          # (direction * volume).cumsum()

    Overwriting these five columns on the finished frame is exact rather than an
    approximation: no downstream stage (order flow, anatomy, structure, regime
    indicators, MTF confluence, regime labels, triple-barrier labels) reads any
    of them, so nothing else in the frame can depend on their values.
    """
    leg = out.copy()
    atr_safe = leg["atr"].replace(0, np.nan)
    day_group = leg["timestamp_utc"] // 86400

    pdh = leg["high"].groupby(day_group).transform("max").shift(288).ffill()
    pdl = leg["low"].groupby(day_group).transform("min").shift(288).ffill()
    leg["dist_pdh_atr"] = ((leg["close"] - pdh) / atr_safe).fillna(0.0)
    leg["dist_pdl_atr"] = ((leg["close"] - pdl) / atr_safe).fillna(0.0)

    asia_mask = leg["session"].str.contains("asia", na=False)
    asia_high = leg["high"].where(asia_mask).groupby(day_group).transform("max").ffill()
    asia_low = leg["low"].where(asia_mask).groupby(day_group).transform("min").ffill()
    leg["dist_asia_high_atr"] = ((leg["close"] - asia_high) / atr_safe).fillna(0.0)
    leg["dist_asia_low_atr"] = ((leg["close"] - asia_low) / atr_safe).fillna(0.0)

    direction = np.sign(leg["close"].diff()).fillna(0)
    leg["obv"] = (direction * leg["volume"]).cumsum().fillna(0)

    return leg


def phase2(cfg, fixed: pd.DataFrame, legacy: pd.DataFrame):
    from model.trainer import FEATURE_COLUMNS

    hr("PHASE 2  feature diff on real history: pre-fix vs fixed")

    print(f"rows              : {len(fixed)}")
    print(f"window (UTC)      : {fmt_ts(fixed['timestamp_utc'].iloc[0])} .. "
          f"{fmt_ts(fixed['timestamp_utc'].iloc[-1])}")
    print(f"columns           : {len(legacy.columns)} -> {len(fixed.columns)}")
    added = [c for c in fixed.columns if c not in legacy.columns]
    removed = [c for c in legacy.columns if c not in fixed.columns]
    print(f"columns added     : {added or 'none'}")
    print(f"columns removed   : {removed or 'none'}")

    # ---- 2a. which trained features moved -----------------------------------
    print("\n2a. FEATURE_COLUMNS changed by the fix\n")
    rows, identical, missing = [], [], []
    for col in FEATURE_COLUMNS:
        if col not in fixed.columns or col not in legacy.columns:
            missing.append(col)
            continue
        a = pd.to_numeric(legacy[col], errors="coerce")
        b = pd.to_numeric(fixed[col], errors="coerce")
        delta = (b - a).abs()
        changed = int((delta > 1e-12).sum())
        if changed == 0:
            identical.append(col)
            continue
        rows.append([col, changed, f"{100.0 * changed / len(fixed):.2f}%",
                     f"{delta.max():.6f}", f"{delta[delta > 1e-12].median():.6f}",
                     f"{delta.mean():.6f}"])

    if rows:
        table(["feature", "changed_bars", "share", "max_abs_delta",
               "median_abs_delta", "mean_abs_delta"], rows)
    else:
        print("  (no differences - unexpected, check that the branch is checked out)")

    print(f"\nbit-for-bit identical ({len(identical)}): {', '.join(identical)}")
    if missing:
        print(f"not present in frame ({len(missing)}): {', '.join(missing)}")

    unexpected = sorted({r[0] for r in rows} - set(CHANGED_FEATURES))
    if unexpected:
        print(f"\n!! UNEXPECTED columns changed: {unexpected}")
        print("   Layer 0 should only move the five session/day/OBV features.")

    # ---- 2b. A1: the change must be confined to open Asian sessions ----------
    print("\n2b. A1 - Asian-session features, broken down by session\n")

    def session_of(value):
        text = str(value)
        for name in ("asia", "london", "newyork"):
            if name in text:
                return name
        return "off_session"

    sess = fixed["session"].map(session_of)
    rows = []
    for col in ("dist_asia_high_atr", "dist_asia_low_atr"):
        delta = (pd.to_numeric(fixed[col], errors="coerce")
                 - pd.to_numeric(legacy[col], errors="coerce")).abs()
        for name in ("asia", "london", "newyork", "off_session"):
            mask = sess == name
            if not mask.any():
                continue
            sub = delta[mask]
            hit = int((sub > 1e-12).sum())
            rows.append([col, name, int(mask.sum()), hit,
                         f"{100.0 * hit / max(int(mask.sum()), 1):.2f}%",
                         f"{sub.max():.4f}", f"{sub.mean():.4f}"])
    table(["feature", "session", "bars", "changed", "share", "max_abs_d_atr",
           "mean_abs_d_atr"], rows)
    print("\nExpected: non-zero inside `asia` only. A changed London/NY bar means the")
    print("completed-session value moved, which the fix must never do.")

    print("\n    worst intra-session leaks (ATR units)\n")
    delta = (pd.to_numeric(fixed["dist_asia_high_atr"], errors="coerce")
             - pd.to_numeric(legacy["dist_asia_high_atr"], errors="coerce")).abs()
    top = delta.nlargest(5)
    table(["utc", "session", "before(leaky)", "after(causal)", "delta_atr"],
          [[fmt_ts(fixed["timestamp_utc"].iloc[i]), sess.iloc[i],
            f"{legacy['dist_asia_high_atr'].iloc[i]:.4f}",
            f"{fixed['dist_asia_high_atr'].iloc[i]:.4f}",
            f"{delta.iloc[i]:.4f}"]
           for i in [fixed.index.get_loc(ix) for ix in top.index]])

    # ---- 2c. A2: what level was dist_pdh_atr actually measured from? ---------
    print("\n2c. A2 - what dist_pdh_atr was really measured from\n")
    day_group = fixed["timestamp_utc"] // 86400
    true_pdh = day_group.map(fixed.groupby(day_group)["high"].max().shift(1))
    atr_col = pd.to_numeric(fixed["atr"], errors="coerce")
    mask = atr_col.notna() & (atr_col > 0) & true_pdh.notna()
    for label, frame in (("before", legacy), ("after ", fixed)):
        recovered = frame["close"] - frame["dist_pdh_atr"] * atr_col
        err = (recovered - true_pdh).abs()[mask]
        hits = int((err < 1e-6).sum())
        print(f"  {label}: bars matching the true previous-day high = {hits} / {int(mask.sum())}"
              f"   (mean |error| = {err.mean():.4f} price units)")
    print("\n  A 0/N score before the fix means the feature never once measured what")
    print("  its name claims: shift(288) is 288 BARS, i.e. 3 days on M15.")

    # ---- 2d. A2: OBV level shift --------------------------------------------
    print("\n2d. A2 - obv distribution (unbounded cumsum -> 100-bar anchor)\n")
    desc = pd.DataFrame({"before": pd.to_numeric(legacy["obv"], errors="coerce").describe(),
                         "after": pd.to_numeric(fixed["obv"], errors="coerce").describe()})
    table(["stat", "before", "after"],
          [[k, f"{desc['before'][k]:,.2f}", f"{desc['after'][k]:,.2f}"] for k in desc.index])

    # ---- 2e. regime labels must not move ------------------------------------
    if "regime" in fixed.columns and "regime" in legacy.columns:
        print("\n2e. regime labels (must be unchanged - Layer 0 touches features only)\n")
        left = fixed["regime"].astype(str).value_counts()
        right = legacy["regime"].astype(str).value_counts()
        keys = sorted(set(left.index) | set(right.index))
        table(["regime", "before", "after"],
              [[k, int(right.get(k, 0)), int(left.get(k, 0))] for k in keys])
        moved = int((fixed["regime"].astype(str) != legacy["regime"].astype(str)).sum())
        print(f"\n  bars whose regime label changed: {moved} / {len(fixed)}")


# ---------------------------------------------------------------------------
# Phase 3 - retrain and measure the shift
# ---------------------------------------------------------------------------
def _feature_importances(model, cols):
    """Best-effort importances through calibration / ensemble wrappers."""
    def dig(obj, depth=0):
        if obj is None or depth > 4:
            return None
        imp = getattr(obj, "feature_importances_", None)
        if imp is not None and len(imp) == len(cols):
            return np.asarray(imp, dtype=float)
        for attr in ("base_estimator", "estimator", "best_estimator_"):
            found = dig(getattr(obj, attr, None), depth + 1)
            if found is not None:
                return found
        for attr in ("calibrated_classifiers_", "estimators_"):
            children = getattr(obj, attr, None)
            if children:
                collected = [dig(c, depth + 1) for c in children]
                collected = [c for c in collected if c is not None]
                if collected:
                    return np.mean(collected, axis=0)
        named = getattr(obj, "named_estimators_", None)
        if named:
            collected = [dig(v, depth + 1) for v in named.values()]
            collected = [c for c in collected if c is not None]
            if collected:
                return np.mean(collected, axis=0)
        return None

    try:
        return dig(model)
    except Exception:
        return None


def _auc(truth, prob):
    """ROC-AUC via sklearn, with a tie-aware rank fallback so the report is never blank."""
    truth = np.asarray(truth)
    prob = np.asarray(prob, dtype=float)
    pos, neg = int(truth.sum()), int((1 - truth).sum())
    if pos == 0 or neg == 0:
        return float("nan")
    try:
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(truth, prob))
    except Exception:
        order = np.argsort(prob, kind="mergesort")
        ranks = np.empty(len(prob), dtype=float)
        ranks[order] = np.arange(1, len(prob) + 1, dtype=float)
        ordered = prob[order]
        i = 0
        while i < len(ordered):          # average the ranks of tied scores
            j = i
            while j + 1 < len(ordered) and ordered[j + 1] == ordered[i]:
                j += 1
            if j > i:
                ranks[order[i:j + 1]] = (i + j + 2) / 2.0
            i = j + 1
        return float((ranks[truth == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def _score(name, model, X_test, y_test, cfg):
    from model.calibration import compute_brier_score, compute_ece

    classes = list(getattr(model, "classes_", []))
    if not classes:
        raise RuntimeError("model exposes no classes_")
    pos_label = max(classes)
    pos_index = classes.index(pos_label)

    prob = np.asarray(model.predict_proba(X_test))[:, pos_index].astype(float)
    truth = (np.asarray(y_test) == pos_label).astype(int)

    out = {"name": name, "pos_label": pos_label, "classes": classes,
           "n": len(truth), "base_rate": float(truth.mean())}

    out["auc"] = _auc(truth, prob)
    out["accuracy"] = float(((prob >= 0.5).astype(int) == truth).mean())
    out["brier"] = float(compute_brier_score(truth, prob))
    out["ece"] = float(compute_ece(truth, prob, n_bins=10)[0])

    gates = {
        "min_ml_probability": cfg.get("ensemble", {}).get("min_ml_probability", 0.55),
        "ml_confidence_floor": cfg.get("ensemble", {}).get("ml_confidence_floor", 0.62),
        "min_confidence_to_alert": cfg.get("assets", {}).get("XAUUSD", {})
                                      .get("min_confidence_to_alert", 0.70),
    }
    out["gates"] = {}
    for gate_name, threshold in gates.items():
        long_sel = prob >= threshold
        short_sel = prob <= (1.0 - threshold)
        out["gates"][gate_name] = {
            "threshold": float(threshold),
            "long_coverage": float(long_sel.mean()),
            "long_precision": float(truth[long_sel].mean()) if long_sel.any() else float("nan"),
            "short_coverage": float(short_sel.mean()),
            "short_precision": float(1.0 - truth[short_sel].mean()) if short_sel.any() else float("nan"),
        }
    return out


def phase3(cfg, fixed: pd.DataFrame, legacy: pd.DataFrame, args):
    from model.trainer import (build_training_matrix, time_ordered_split,
                               train_model, calibrate_model, save_model)

    hr("PHASE 3  retrain on the fixed features and measure the shift")
    print("NOT a deployment run. Identical config, identical time-ordered split,")
    print("identical seed; the only difference is the value of five columns.\n")

    built = {}
    for label, frame in (("before", legacy), ("after", fixed)):
        X, y, cols = build_training_matrix(frame, cfg=cfg)
        built[label] = (X, y, cols)
        print(f"  {label:<6} labeled rows={len(X):<7} features={len(cols):<4} "
              f"class_counts={y.value_counts().to_dict()}")

    X_b, y_b, cols_b = built["before"]
    X_a, y_a, cols_a = built["after"]

    if list(cols_b) != list(cols_a):
        print(f"\n  !! feature lists differ:")
        print(f"     only before: {sorted(set(cols_b) - set(cols_a))}")
        print(f"     only after : {sorted(set(cols_a) - set(cols_b))}")
    if len(X_b) != len(X_a) or not np.array_equal(np.asarray(y_b), np.asarray(y_a)):
        print("\n  !! label vectors differ between the two runs - they must not.")
        print("     Labels are price-based, so this points at a data problem.")

    if len(X_a) < 500:
        print(f"\n  Not enough labeled rows to train ({len(X_a)} < 500). Phase 3 stops here.")
        return None

    train_ratio = cfg["model"].get("train_ratio", 0.8)
    results, models = {}, {}
    for label in ("before", "after"):
        X, y, cols = built[label]
        X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio)
        print(f"\n  training [{label}]  train={len(X_train)}  test={len(X_test)} ...")
        base = train_model(X_train, y_train, cfg)
        calibrated = calibrate_model(base, X_train, y_train, cfg)
        models[label] = (base, calibrated, cols)
        results[label] = _score(label, calibrated, X_test, y_test, cfg)
        if args.save_models:
            path = os.path.join(args.model_dir, f"layer0_{label}.joblib")
            os.makedirs(args.model_dir, exist_ok=True)
            save_model(calibrated, cols, path)
            print(f"    saved -> {path}")

    before, after = results["before"], results["after"]
    print(f"\n  positive class = {after['pos_label']!r} of {after['classes']}, "
          f"test rows = {after['n']}, base rate = {after['base_rate']:.4f}\n")

    print("3a. headline metrics (test fold)\n")
    table(["metric", "before (leaky)", "after (causal)", "delta"],
          [[key,
            f"{before[key]:.4f}",
            f"{after[key]:.4f}",
            f"{after[key] - before[key]:+.4f}"]
           for key in ("auc", "accuracy", "brier", "ece")])
    print("\n  Higher is better for auc/accuracy, LOWER is better for brier/ece.")
    print("  A drop in AUC is the expected, healthy outcome: the 'before' number was")
    print("  partly earned by reading the future. It is a measurement of how much of")
    print("  the reported edge was an artefact, not a regression to fix.")

    print("\n3b. precision at the production gates\n")
    rows = []
    for gate_name in before["gates"]:
        gb, ga = before["gates"][gate_name], after["gates"][gate_name]
        rows.append([gate_name, f"{gb['threshold']:.2f}",
                     f"{gb['long_precision']:.4f}", f"{ga['long_precision']:.4f}",
                     f"{gb['long_coverage']:.4f}", f"{ga['long_coverage']:.4f}"])
        rows.append([f"{gate_name} (short)", f"{1 - gb['threshold']:.2f}",
                     f"{gb['short_precision']:.4f}", f"{ga['short_precision']:.4f}",
                     f"{gb['short_coverage']:.4f}", f"{ga['short_coverage']:.4f}"])
    table(["gate", "thr", "prec_before", "prec_after", "cov_before", "cov_after"], rows)

    print("\n3c. how much the model leaned on the five changed features\n")
    for label in ("before", "after"):
        base, calibrated, cols = models[label]
        imp = _feature_importances(base, cols)
        if imp is None:
            imp = _feature_importances(calibrated, cols)
        if imp is None:
            print(f"  [{label}] feature importances unavailable for this model type")
            continue
        total = float(imp.sum()) or 1.0
        order = np.argsort(imp)[::-1]
        rank = {cols[i]: pos + 1 for pos, i in enumerate(order)}
        share = sum(imp[list(cols).index(c)] for c in CHANGED_FEATURES if c in cols) / total
        print(f"  [{label}] the five changed features hold {share * 100:.2f}% of total importance")
        table(["feature", "rank", "importance", "share"],
              [[c, rank.get(c, "-"),
                f"{imp[list(cols).index(c)]:.6f}" if c in cols else "-",
                f"{imp[list(cols).index(c)] / total * 100:.2f}%" if c in cols else "-"]
               for c in CHANGED_FEATURES])
        print(f"    top 10 overall: "
              f"{', '.join(f'{cols[i]}({imp[i] / total * 100:.1f}%)' for i in order[:10])}\n")

    print("  If the leaky features sat near the top of the 'before' ranking, the old")
    print("  model was load-bearing on look-ahead and every historical metric derived")
    print("  from it should be treated as void.")

    print("\n  Caveat: this is a plain time-ordered split with NO purge and NO embargo")
    print(f"  (backtest.walk_forward.embargo_candles is currently "
          f"{cfg['backtest']['walk_forward'].get('embargo_candles')}), so labels")
    print("  spanning the split boundary still overlap both folds. Layer 1 (A4-A5)")
    print("  fixes that; until then read these numbers as relative, not absolute.")

    return results


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Layer 0 (A1-A3) validation on real MT5 data.")
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--symbol", default="XAUUSD",
                        help="Symbol as stored in the database (e.g. GOLD)")
    parser.add_argument("--asset-key", default=None,
                        help="Key under config.assets (defaults to --symbol)")
    parser.add_argument("--timeframe", default=None,
                        help="Defaults to the asset's configured timeframe")
    parser.add_argument("--base-timeframe", default="M5",
                        help="Timeframe for the length-agnostic tests in phase 1")
    parser.add_argument("--max-bars", type=int, default=0,
                        help="Use only the last N candles (0 = all)")
    parser.add_argument("--out", default=None, help="Also write the report here")
    parser.add_argument("--save-models", action="store_true",
                        help="Persist both models (off by default - this is not a deploy)")
    parser.add_argument("--model-dir", default=os.path.join("output", "layer0_validation"))
    parser.add_argument("--no-db-copy", action="store_true",
                        help="Read the database in place instead of a scratch copy")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-diff", action="store_true")
    parser.add_argument("--skip-train", action="store_true")
    args = parser.parse_args()

    out_path = args.out or os.path.join(
        "output", f"layer0_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tee = _Tee(out_path)
    sys.stdout = tee

    scratch = None
    try:
        hr("LAYER 0 (A1-A3) VALIDATION")
        print(f"generated      : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"repo root      : {REPO_ROOT}")
        print(f"python         : {sys.version.split()[0]} on {platform.platform()}")
        for mod_name in ("numpy", "pandas", "sklearn", "xgboost", "scipy", "joblib", "pytest"):
            try:
                print(f"  {mod_name:<8} {__import__(mod_name).__version__}")
            except Exception:
                print(f"  {mod_name:<8} NOT INSTALLED")
        try:
            rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                 capture_output=True, text=True).stdout.strip()
            branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT,
                                    capture_output=True, text=True).stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                                   capture_output=True, text=True).stdout.strip()
            print(f"git            : {branch} @ {rev}{'  (dirty)' if dirty else ''}")
        except Exception:
            pass

        from config.loader import load_config
        from data.storage import read_candles

        cfg = load_config()
        asset_key = args.asset_key or args.symbol
        asset_cfg = cfg.get("assets", {}).get(asset_key, {})
        timeframe = args.timeframe or asset_cfg.get("timeframe", "M15")
        print(f"db symbol      : {args.symbol}")
        print(f"config asset   : {asset_key}   timeframe: {timeframe}")
        if not asset_cfg:
            print(f"WARNING: config.assets has no entry for '{asset_key}' - "
                  f"labeling/gate parameters will fall back to defaults.")
        print(f"database       : {args.db_path}")

        if not os.path.exists(args.db_path):
            print(f"\nFATAL: {args.db_path} does not exist.")
            return 2

        db_in_use = args.db_path
        if not args.no_db_copy:
            scratch = tempfile.mkdtemp(prefix="layer0_db_")
            db_in_use = os.path.join(scratch, os.path.basename(args.db_path))
            shutil.copy2(args.db_path, db_in_use)
            for suffix in ("-wal", "-shm"):
                if os.path.exists(args.db_path + suffix):
                    shutil.copy2(args.db_path + suffix, db_in_use + suffix)
            size_mb = os.path.getsize(db_in_use) / 1e6
            print(f"read from copy : {db_in_use}  ({size_mb:.1f} MB; original untouched)")

        # Inventory first: guessing the stored symbol name wastes a whole run.
        try:
            import sqlite3
            con = sqlite3.connect(db_in_use)
            tabs = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            print("\ndatabase inventory  (table / symbol / rows / first .. last)")
            for t in tabs:
                cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
                if "symbol" not in cols or "timestamp_utc" not in cols:
                    print(f"  {t:<26} -- not an ohlcv table")
                    continue
                rows = con.execute(
                    f'SELECT symbol, COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc) '
                    f'FROM "{t}" GROUP BY symbol ORDER BY COUNT(*) DESC').fetchall()
                if not rows:
                    print(f"  {t:<26} -- empty")
                for sym, n, lo, hi in rows:
                    print(f"  {t:<26} {str(sym):<12} {n:>8}  "
                          f"{fmt_ts(lo)} .. {fmt_ts(hi)}")
            con.close()
        except Exception as exc:
            print(f"  (inventory failed: {exc})")

        if not args.skip_tests:
            phase1(args, db_in_use)

        if args.skip_diff and args.skip_train:
            return 0

        hr("BUILDING THE FEATURE FRAME ON REAL HISTORY")
        raw = read_candles(db_in_use, timeframe, args.symbol)
        if raw.empty:
            print(f"FATAL: no candles for {args.symbol} {timeframe} in the database.")
            print("Pick a symbol/timeframe from the inventory printed above, e.g.")
            print("  --symbol GOLD --asset-key XAUUSD --timeframe M15")
            return 2
        if args.max_bars and len(raw) > args.max_bars:
            raw = raw.tail(args.max_bars).reset_index(drop=True)
        print(f"raw candles    : {len(raw)}  "
              f"({fmt_ts(raw['timestamp_utc'].iloc[0])} .. {fmt_ts(raw['timestamp_utc'].iloc[-1])})")

        from scripts.train_mt5 import build_full_df
        fixed = build_full_df(raw, cfg, db_path=db_in_use, asset_key=asset_key,
                              timeframe=timeframe)
        legacy = legacy_reference(fixed)
        print(f"featured rows  : {len(fixed)}")

        if not args.skip_diff:
            phase2(cfg, fixed, legacy)

        if not args.skip_train:
            try:
                phase3(cfg, fixed, legacy, args)
            except Exception:
                hr("PHASE 3 FAILED", char="!")
                traceback.print_exc(file=sys.stdout)
                print("\nSend this traceback along with the rest of the report.")

        hr("DONE")
        print(f"report written to {out_path}")
        return 0
    finally:
        sys.stdout = tee._stdout
        tee.close()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
