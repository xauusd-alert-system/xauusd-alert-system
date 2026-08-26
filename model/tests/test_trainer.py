"""
Unit tests for model/trainer.py and model/predictor.py.
Run with: pytest model/tests/test_trainer.py -v
"""
import os
import sys
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles
from features.indicators import build_all_indicators
from features.candle_anatomy import candle_anatomy
from regime.classifier import add_regime_indicators, classify_regime_series, RegimeLabel
from labeling.label_generator import generate_labels_from_config
from model.trainer import build_training_matrix, time_ordered_split, train_model, calibrate_model, save_model, load_model, FEATURE_COLUMNS
from model.predictor import ModelPredictor

CFG = load_config()
SESSIONS = CFG["sessions"]


# Override labeling barriers to match mock data scale (~0.15 pts/candle drift)
import copy
CFG = copy.deepcopy(CFG)
CFG["labeling"]["target_pips_x"] = 3.0
CFG["labeling"]["stop_pips_y"] = 2.0

def _full_featured_labeled_df(n=3000, seed=55):
    df = fetch_mock_candles("M15", n_candles=n, sessions_config=SESSIONS, seed=seed)
    df = build_all_indicators(df, CFG)
    df = candle_anatomy(df)
    df = add_regime_indicators(df, CFG)
    df["mtf_confluence_score"] = 0.0  # placeholder in isolated test - real value comes from mtf_confluence.py in the full pipeline
    labels = generate_labels_from_config(df, CFG)
    df["label"] = labels
    return df


def _full_featured_labeled_df_regime(n=3000, seed=55):
    """Full-featured + labeled DF that ALSO carries the causal, rule-based `regime`
    column (as real pipeline DataFrames do after classify_regime_series), required by
    the Phase 3 regime-as-feature path."""
    df = _full_featured_labeled_df(n=n, seed=seed)
    df["regime"] = classify_regime_series(df, CFG)
    return df


def test_build_training_matrix_drops_nan_and_zero_labels():
    df = _full_featured_labeled_df()
    X, y, cols = build_training_matrix(df)
    assert len(X) == len(y)
    assert set(y.unique()) <= {0, 1}
    assert not X.isnull().any().any()
    assert len(X) < len(df)  # some rows dropped (NaN warmup rows, label==0 rows, NaN label tail)


def test_build_training_matrix_three_class_keeps_zero_label():
    """Phase 2: with model.include_zero_class=true the label 0 ('no clear outcome') is
    kept as a THIRD class instead of being dropped. y must be encoded
    {0: short, 1: no_trade, 2: long} and must map EXACTLY from the source label
    column: +1 -> 2, -1 -> 0, 0 -> 1."""
    base = _full_featured_labeled_df()
    # Deterministic label mix independent of mock-data randomness: the default mock
    # series happens to hit a barrier on every row (no label 0), so force a known
    # +1 / -1 / 0 pattern onto the non-NaN label positions. Rows whose features are
    # NaN (warmup/tail) are still dropped inside build_training_matrix, which is
    # fine - we only require label 0 to survive among the KEPT rows.
    valid_idx = base["label"].dropna().index
    pattern = [1, -1, 0] * (len(valid_idx) // 3 + 1)
    df = base.copy()
    df.loc[valid_idx, "label"] = pattern[: len(valid_idx)]

    import copy
    three_cfg = copy.deepcopy(CFG)
    three_cfg["model"] = dict(three_cfg.get("model", {}))
    three_cfg["model"]["include_zero_class"] = True

    X, y, cols = build_training_matrix(df, cfg=three_cfg)
    assert len(X) == len(y)
    assert not X.isnull().any().any()
    assert not y.isnull().any()
    assert set(y.unique()) <= {0, 1, 2}

    # Exact semantic mapping check: every retained row's y value must match the
    # source label{+1->2, -1->0, 0->1}. Row indices are preserved by the build, so
    # we can align the source label column back.
    src = df.loc[X.index, "label"]
    expected = src.map(lambda v: {1: 2, -1: 0, 0: 1}.get(int(v), 1))
    assert (y.astype(int).values == expected.values.astype(int)).all()

    # The no_trade class (encoded 1) must actually be represented, proving label 0
    # was NOT dropped (a 1/3 of the kept rows are forced to label 0 -> encoded 1).
    assert (y == 1).any(), "no_trade class (encoded 1) must be present when label 0 is kept"

    # Binary mode drops label 0 entirely and encodes +1->1, -1->0 (no no_trade
    # class): label 0 rows must NOT appear among the retained rows, and the
    # encoding must map exactly.
    X_bin, y_bin, _ = build_training_matrix(df)
    assert len(X) > len(X_bin)
    assert set(y_bin.unique()) <= {0, 1}
    src_bin = df.loc[X_bin.index, "label"]
    assert set(src_bin.unique()) <= {1, -1}, "binary mode must drop label-0 (no_trade) rows"
    expected_bin = (src_bin == 1).astype(int)
    assert (y_bin.astype(int).values == expected_bin.values).all()


def test_build_training_matrix_regime_feature_gates():
    """Phase 3: with model.use_regime_feature=true the causal `regime` column is
    expanded into fixed one-hot regime_<label> features (full RegimeLabel set)
    appended to the training matrix with no NaN and exactly one active bit per row.
    Off-mode (default) must leave the Phase-0+1 feature set completely untouched
    (no regime_* columns), and a missing `regime` column must raise a clear error."""
    df = _full_featured_labeled_df_regime()
    regime_labels = [r for r in RegimeLabel]

    # Off-mode (default): feature set identical to Phase 0+1 - no regime_* columns.
    X_off, _, cols_off = build_training_matrix(df, cfg=CFG)
    assert not any(c.startswith("regime_") for c in cols_off)
    assert not any(c.startswith("regime_") for c in X_off.columns)

    # On-mode: full one-hot set, fully populated ints, exactly one active bit/row.
    reg_cfg = copy.deepcopy(CFG)
    reg_cfg["model"] = dict(reg_cfg.get("model", {}))
    reg_cfg["model"]["use_regime_feature"] = True
    X, y, cols = build_training_matrix(df, cfg=reg_cfg)
    assert len(X) == len(y)
    assert not X.isnull().any().any()
    expected_cols = [f"regime_{lab.value}" for lab in regime_labels]
    for c in expected_cols:
        assert c in cols, f"expected regime feature {c} in training cols"
        assert c in X.columns
    onehot = X[expected_cols]
    assert (onehot.min().min() >= 0) and (onehot.max().max() <= 1)
    assert (onehot.sum(axis=1) == 1).all(), "every retained row has exactly one active regime bit"

    # The active bit must match the raw regime label 1:1 for every retained row.
    src = df.loc[X.index, "regime"]
    for lab in regime_labels:
        bit = onehot[f"regime_{lab.value}"].astype(bool).values
        assert (bit == (src == lab).values).all(), f"regime_{lab.value} bit must match raw regime"

    # Missing `regime` column with the flag on must fail loudly, not silently train
    # a model that would KeyError at inference time.
    no_reg = df.drop(columns=["regime"])
    with pytest.raises(ValueError, match="use_regime_feature"):
        build_training_matrix(no_reg, cfg=reg_cfg)


def test_regime_feature_train_and_predict_auto_synth_end_to_end(tmp_path):
    """Phase 3: a model trained with use_regime_feature=true saves regime_<label>
    feature cols. Predicting on a DataFrame that carries only the raw causal `regime`
    column (exactly what run_backtest / diag_fx_slippage / realtime pipeline feed it)
    must AUTO-SYNTHESIZE the missing one-hot columns inside ModelPredictor and produce
    valid p_short/p_long - never a KeyError or a missing-column ValueError."""
    df = _full_featured_labeled_df_regime(n=5000, seed=77)
    reg_cfg = copy.deepcopy(CFG)
    reg_cfg["model"] = dict(reg_cfg.get("model", {}))
    reg_cfg["model"]["use_regime_feature"] = True

    X, y, cols = build_training_matrix(df, cfg=reg_cfg)
    X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio=0.8)
    if len(X_train) < 30 or len(X_test) < 5 or y_train.nunique() < 2:
        pytest.skip("Insufficient class diversity for this seed - not a code defect")

    base_model = train_model(X_train, y_train, reg_cfg)
    calibrated = calibrate_model(base_model, X_train, y_train, reg_cfg)
    model_path = str(tmp_path / "test_regime_model.joblib")
    save_model(calibrated, cols, model_path)
    predictor = ModelPredictor(model_path)
    assert any(c.startswith("regime_") for c in predictor.feature_cols)

    # Consumer-style raw frame: has `regime`, NO expanded regime_* columns.
    raw = df.iloc[X_test.index].copy()
    assert "regime" in raw.columns
    assert not any(c.startswith("regime_") for c in raw.columns)

    preds = predictor.predict_proba(raw)
    assert len(preds) == len(X_test)
    assert np.allclose(preds["p_long"] + preds["p_short"], 1.0, atol=1e-6)
    assert (preds["p_long"] >= 0).all() and (preds["p_long"] <= 1).all()

    # Single-row path used by realtime/pipeline.generate_signal works too.
    single = predictor.predict_single(raw.iloc[0])
    assert 0.0 <= single["p_long"] <= 1.0
    assert 0.0 <= single["p_short"] <= 1.0


def test_time_ordered_split_preserves_order():
    df = _full_featured_labeled_df()
    X, y, cols = build_training_matrix(df)
    X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio=0.8)
    assert X_train.index.max() < X_test.index.min(), "Train indices must all precede test indices (no shuffling)"
    assert len(X_train) + len(X_test) == len(X)


def test_train_and_predict_end_to_end(tmp_path):
    df = _full_featured_labeled_df(n=4000, seed=77)
    X, y, cols = build_training_matrix(df)
    X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio=0.8)

    if len(X_train) < 30 or len(X_test) < 5 or y_train.nunique() < 2:
        pytest.skip("Insufficient class diversity in mock data for this seed - not a code defect")

    base_model = train_model(X_train, y_train, CFG)
    calibrated = calibrate_model(base_model, X_train, y_train, CFG)

    model_path = str(tmp_path / "test_model.joblib")
    save_model(calibrated, cols, model_path)

    predictor = ModelPredictor(model_path)
    preds = predictor.predict_proba(X_test)

    assert len(preds) == len(X_test)
    assert np.allclose(preds["p_long"] + preds["p_short"], 1.0, atol=1e-6)
    assert (preds["p_long"] >= 0).all() and (preds["p_long"] <= 1).all()


def test_three_class_train_and_predict_end_to_end(tmp_path):
    """Phase 2: a model trained with include_zero_class=true (y in {0: short, 1:
    no_trade, 2: long}) must be detected as 3-class at inference and expose
    p_short + p_no_trade + p_long (summing to 1.0) from predict_proba and
    p_no_trade from predict_single, while p_long/p_short stay the same semantics
    as the binary model (so downstream consumers are unchanged)."""
    df = _full_featured_labeled_df(n=5000, seed=77)
    valid_idx = df["label"].dropna().index
    pattern = [1, -1, 0] * (len(valid_idx) // 3 + 1)
    df.loc[valid_idx, "label"] = pattern[: len(valid_idx)]

    three_cfg = copy.deepcopy(CFG)
    three_cfg["model"] = dict(three_cfg.get("model", {}))
    three_cfg["model"]["include_zero_class"] = True

    X, y, cols = build_training_matrix(df, cfg=three_cfg)
    X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio=0.8)

    if len(X_train) < 30 or len(X_test) < 5 or set(y_train.unique()) != {0, 1, 2}:
        pytest.skip("Insufficient 3-class diversity in this seed - not a code defect")

    base_model = train_model(X_train, y_train, three_cfg)
    calibrated = calibrate_model(base_model, X_train, y_train, three_cfg)

    model_path = str(tmp_path / "test_model3.joblib")
    save_model(calibrated, cols, model_path)

    predictor = ModelPredictor(model_path)
    preds = predictor.predict_proba(X_test)

    assert len(preds) == len(X_test)
    assert {"p_short", "p_no_trade", "p_long"} <= set(preds.columns)
    assert np.allclose(preds["p_short"] + preds["p_no_trade"] + preds["p_long"], 1.0, atol=1e-6)
    assert (preds["p_no_trade"] >= 0).all() and (preds["p_no_trade"] <= 1).all()

    # predict_single surfaces p_no_trade (realtime/pipeline uses this dict).
    single = predictor.predict_single(X_test.iloc[0])
    assert "p_no_trade" in single
    assert 0.0 <= single["p_no_trade"] <= 1.0


def test_calibrate_model_uses_purged_time_ordered_split_not_shuffled_kfold():
    """Phase 0+1: calibration must NOT use CalibratedClassifierCV's default internal
    shuffled K-fold (which leaks model-fit rows into the calibrator across the label
    horizon). It must fit the base model on an EARLIER slice, leave a purge gap >= the
    labeling horizon, and fit the calibrator on a strictly LATER trailing slice via an
    explicit time-ordered split passed as cv (never sklearn's int cv=3 shuffle)."""
    df = _full_featured_labeled_df(n=5000, seed=33)
    X, y, cols = build_training_matrix(df)
    if len(X) < 400 or y.nunique() < 2:
        pytest.skip("Insufficient data for this seed - not a code defect")

    base = train_model(X, y, CFG)
    calibrated = calibrate_model(base, X, y, CFG)

    # Honest path must return a CalibratedClassifierCV with exactly ONE calibrator
    # (single time-ordered split), not 3 calibrators from a K-fold.
    assert hasattr(calibrated, "calibrated_classifiers_")
    assert len(calibrated.calibrated_classifiers_) == 1
    assert not getattr(calibrated, "_is_honest_placeholder", False)

    # cv must be a single explicit time-ordered split, not an int (sklearn's default
    # K-fold would shuffle). Expose the split indices to assert strict ordering.
    cv = calibrated.cv
    assert isinstance(cv, list) and len(cv) == 1
    train_idx, test_idx = cv[0]
    assert (np.max(train_idx) + 1) <= np.min(test_idx), (
        "calibration fit indices must all precede calibration held-out indices"
    )
    # A purge gap >= labeling horizon must separate the fit set from the calibrator set.
    horizon = int(CFG["labeling"]["horizon_candles_n"])
    assert horizon > 0
    assert np.min(test_idx) - np.max(train_idx) - 1 >= horizon


def test_calibrate_model_degenerates_to_identity_placeholder_on_tiny_data():
    """When the training set is too small for a valid purged split, calibrate_model
    must NEVER fall back to a shuffled K-fold - it degrades to an identity (no-op)
    calibration wrapper so raw probabilities are used instead of leaked ones."""
    df = _full_featured_labeled_df(n=400, seed=11)
    X, y, cols = build_training_matrix(df)
    if y.nunique() < 2 or len(X) < 30:
        pytest.skip("Insufficient class diversity - not a code defect")

    # Force the degenerate path by slicing to a tiny training set.
    tiny = X.iloc[:80]
    tiny_y = y.iloc[:80]
    base = train_model(tiny, tiny_y, CFG)
    result = calibrate_model(base, tiny, tiny_y, CFG)

    assert getattr(result, "_is_honest_placeholder", False) is True


def test_predictor_raises_on_missing_feature_columns(tmp_path):
    df = _full_featured_labeled_df(n=2000, seed=99)
    X, y, cols = build_training_matrix(df)
    if y.nunique() < 2 or len(X) < 30:
        pytest.skip("Insufficient class diversity in mock data for this seed - not a code defect")

    base_model = train_model(X, y, CFG)
    calibrated = calibrate_model(base_model, X, y, CFG)
    model_path = str(tmp_path / "test_model2.joblib")
    save_model(calibrated, cols, model_path)

    predictor = ModelPredictor(model_path)
    incomplete_row = X.iloc[0].drop(cols[0])  # remove one required feature
    with pytest.raises(ValueError):
        predictor.predict_proba(incomplete_row)


def test_predictor_raises_on_nan_input(tmp_path):
    df = _full_featured_labeled_df(n=2000, seed=123)
    X, y, cols = build_training_matrix(df)
    if y.nunique() < 2 or len(X) < 30:
        pytest.skip("Insufficient class diversity in mock data for this seed - not a code defect")

    base_model = train_model(X, y, CFG)
    calibrated = calibrate_model(base_model, X, y, CFG)
    model_path = str(tmp_path / "test_model3.joblib")
    save_model(calibrated, cols, model_path)

    predictor = ModelPredictor(model_path)
    bad_row = X.iloc[0].copy()
    bad_row[cols[0]] = np.nan
    with pytest.raises(ValueError):
        predictor.predict_proba(bad_row)


# ---------------------------------------------------------------------------
# Regression: three-class label space with a MISSING class.
#
# build_training_matrix encodes the three-class model as {0: short, 1: no_trade,
# 2: long}. XGBoost's sklearn wrapper infers n_classes from np.unique(y) and
# demands contiguous labels [0..n-1], so a training window holding no no_trade
# rows arrives as classes [0, 2] and fit() died with
#     ValueError: Invalid classes inferred from unique values of `y`.
#                 Expected: [0 1], got [0 2]
# which aborted the entire GBPUSD (the three-class asset) walk-forward run.
# ---------------------------------------------------------------------------

from model.trainer import (  # noqa: E402
    normalize_label_space,
    DegenerateLabelSpaceError,
)

THREE_CLASS_CFG = {
    "model": {"include_zero_class": True, "type": "xgboost",
              "calibration_method": "sigmoid", "random_seed": 42},
    "labeling": {"horizon_candles_n": 36},
}
BINARY_CFG = {
    "model": {"include_zero_class": False, "type": "xgboost",
              "calibration_method": "sigmoid", "random_seed": 42},
    "labeling": {"horizon_candles_n": 36},
}


def _directional_xy(n=600, seed=7):
    """Feature 'a' fully determines the direction, so p_long must track it."""
    rng = np.random.default_rng(seed)
    a = rng.normal(size=n)
    X = pd.DataFrame({"ema_9": a, "rsi": rng.normal(size=n),
                      "atr": rng.normal(size=n), "macd_line": rng.normal(size=n)})
    return a, X


def test_normalize_label_space_passthrough_cases():
    """Full three-class and plain binary spaces are already contiguous and must
    be handed to the model untouched (no silent relabeling)."""
    y3 = pd.Series([0, 1, 2] * 10)
    assert sorted(normalize_label_space(y3, THREE_CLASS_CFG).unique()) == [0, 1, 2]

    y2 = pd.Series([0, 1] * 15)
    assert sorted(normalize_label_space(y2, BINARY_CFG).unique()) == [0, 1]


def test_normalize_label_space_without_no_trade_remaps_to_binary():
    """{0: short, 2: long} (no no_trade row in the window) must collapse onto the
    binary encoding {0: short, 1: long} that ModelPredictor already decodes,
    preserving WHICH rows are long and which are short."""
    y = pd.Series([0, 2, 2, 0, 2])
    out = normalize_label_space(y, THREE_CLASS_CFG)
    assert sorted(out.unique()) == [0, 1]
    # short rows stay short, long rows stay long (order preserved, not inverted)
    assert list(out) == [0, 1, 1, 0, 1]


def test_normalize_label_space_refuses_when_a_direction_is_missing():
    """no_trade present but an entire DIRECTION absent: the fitted model's second
    probability column would be P(no_trade), which the binary decode path would
    report as p_long/p_short - a silently wrong directional probability. Must
    raise instead so the caller can skip the window."""
    for labels in ([0, 1, 1, 0], [1, 2, 2, 1]):
        with pytest.raises(DegenerateLabelSpaceError):
            normalize_label_space(pd.Series(labels), THREE_CLASS_CFG)


def test_normalize_label_space_refuses_single_class():
    with pytest.raises(DegenerateLabelSpaceError):
        normalize_label_space(pd.Series([1, 1, 1]), THREE_CLASS_CFG)
    with pytest.raises(DegenerateLabelSpaceError):
        normalize_label_space(pd.Series([0, 0, 0]), BINARY_CFG)


def test_three_class_window_without_no_trade_trains_and_keeps_direction(tmp_path):
    """End-to-end regression for the GBPUSD crash: a three-class config whose
    window contains only short/long rows must now train, calibrate, save and
    predict - and p_long must still mean LONG."""
    a, X = _directional_xy()
    y = pd.Series(np.where(a > 0, 2, 0), index=X.index)  # {0, 2}: no no_trade
    assert sorted(y.unique()) == [0, 2]

    base = train_model(X, y, THREE_CLASS_CFG)          # used to raise ValueError
    assert list(base.classes_) == [0, 1]
    assert base._label_space_no_trade_absent is True

    calibrated = calibrate_model(base, X, y, THREE_CLASS_CFG)
    model_path = str(tmp_path / "degenerate_three_class.joblib")
    save_model(calibrated, list(X.columns), model_path)

    preds = ModelPredictor(model_path).predict_proba(X)
    # Decoded on the binary path: no no_trade mass was ever observed.
    assert set(preds.columns) == {"p_short", "p_long"}
    assert np.allclose(preds["p_short"] + preds["p_long"], 1.0, atol=1e-6)
    # Direction semantics preserved through the remap.
    assert preds.loc[a > 1.0, "p_long"].mean() > preds.loc[a < -1.0, "p_long"].mean()


def test_full_three_class_window_still_exposes_no_trade(tmp_path):
    """The remap must NOT leak into healthy windows: with all three classes
    present the model stays 3-class and still reports p_no_trade."""
    a, X = _directional_xy(seed=11)
    y = pd.Series(np.select([a > 0.5, a < -0.5], [2, 0], default=1), index=X.index)
    assert sorted(y.unique()) == [0, 1, 2]

    base = train_model(X, y, THREE_CLASS_CFG)
    assert list(base.classes_) == [0, 1, 2]
    assert base._label_space_no_trade_absent is False

    calibrated = calibrate_model(base, X, y, THREE_CLASS_CFG)
    model_path = str(tmp_path / "healthy_three_class.joblib")
    save_model(calibrated, list(X.columns), model_path)

    preds = ModelPredictor(model_path).predict_proba(X)
    assert {"p_short", "p_no_trade", "p_long"} == set(preds.columns)
    assert np.allclose(preds.sum(axis=1), 1.0, atol=1e-6)


def test_save_model_injects_self_hash_and_detects_tampering(tmp_path):
    """save_model embeds a deterministic content fingerprint (metadata.model_hash)
    so the artifact carries its own identity; the fingerprint is reproducible
    after reload and a tampered stored hash is detected."""
    from model.trainer import compute_model_fingerprint

    df = _full_featured_labeled_df_regime(n=1200, seed=7)
    X, y, cols = build_training_matrix(df, cfg=CFG)
    X_train, X_test, y_train, y_test = time_ordered_split(X, y, train_ratio=0.8)
    if len(X_train) < 30 or y_train.nunique() < 2:
        pytest.skip("Insufficient class diversity for this seed - not a code defect")

    base = train_model(X_train, y_train, CFG)
    calibrated = calibrate_model(base, X_train, y_train, CFG)
    metadata = {"trained_at_utc": "2026-08-26T00:00:00+00:00", "note": "self-hash test"}
    model_path = str(tmp_path / "self_hash_model.joblib")
    save_model(calibrated, cols, model_path, metadata=metadata)

    loaded = load_model(model_path)
    stored_hash = loaded["metadata"]["model_hash"]
    assert stored_hash, "save_model must inject metadata.model_hash"
    # Original metadata keys survive alongside the injected hash.
    assert loaded["metadata"]["trained_at_utc"] == metadata["trained_at_utc"]

    # Deterministic and reproducible after a fresh load.
    assert stored_hash == compute_model_fingerprint(calibrated, cols)
    assert stored_hash == compute_model_fingerprint(
        load_model(model_path)["model"], load_model(model_path)["feature_cols"])

    # A bundle whose stored hash no longer matches its content is caught.
    tampered_meta = dict(loaded["metadata"])
    tampered_meta["model_hash"] = "0" * 64
    tampered_path = str(tmp_path / "tampered_model.joblib")
    save_model(loaded["model"], loaded["feature_cols"], tampered_path, metadata=tampered_meta)
    tampered = load_model(tampered_path)
    assert tampered["metadata"]["model_hash"] != compute_model_fingerprint(
        tampered["model"], tampered["feature_cols"])
