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
from regime.classifier import add_regime_indicators
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


def test_build_training_matrix_drops_nan_and_zero_labels():
    df = _full_featured_labeled_df()
    X, y, cols = build_training_matrix(df)
    assert len(X) == len(y)
    assert set(y.unique()) <= {0, 1}
    assert not X.isnull().any().any()
    assert len(X) < len(df)  # some rows dropped (NaN warmup rows, label==0 rows, NaN label tail)


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
