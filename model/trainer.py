"""
Gradient boosting model trainer (XGBoost) for directional bias prediction.

CRITICAL NO-LOOK-AHEAD DESIGN:
Training data (X, y) pairs are built by joining features/ (computed causally, see
features/tests/test_no_lookahead.py) with labeling/ (which is INTENTIONALLY forward-
looking, see labeling/label_generator.py docstring). The join is valid for TRAINING
purposes only: row i's features (known at close of candle i) are paired with row i's
label (the future outcome after candle i) - this is exactly how supervised learning
is supposed to work. The critical invariant is that this label column must NEVER be
fed back into features/ or regime/ as an input, and must NEVER be available to
realtime/pipeline.py at inference time (it doesn't exist yet, by construction, live).

Train/test split is TIME-ORDERED (not shuffled) to respect temporal causality -
shuffling would let the model implicitly learn from "future" rows relative to some
test rows, which is a subtle but real leakage risk in time series ML.

Probability calibration (isotonic or sigmoid) is applied on top of raw XGBoost
probabilities because tree ensembles are often poorly calibrated out-of-the-box -
calibration is essential here because the ensemble layer (Step 9) and alert
thresholds rely on P(long)/P(short) being genuinely interpretable probabilities,
not just rank-ordered scores.
"""
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.base import BaseEstimator, ClassifierMixin
import joblib
import os


FEATURE_COLUMNS = [
    "ema_9", "ema_21", "ema_50", "ema_200", "rsi", "macd_line", "macd_signal", "macd_hist",
    "atr", "bb_width", "body_ratio", "upper_wick_ratio", "lower_wick_ratio", "candle_direction",
    "adx", "plus_di", "minus_di", "atr_ratio", "mtf_confluence_score",
]


def build_training_matrix(df: pd.DataFrame, label_col: str = "label") -> tuple:
    """
    Extracts (X, y) from a fully-featured, labeled DataFrame.
    Drops rows with NaN in required feature columns or NaN label (label is NaN
    near the end of the dataset where insufficient future data existed, per
    labeling/label_generator.py - these rows are correctly excluded from training).
    Only binary direction is modeled here (label 0 = "no clear outcome" is dropped
    from the classifier's training set; the meta-filter/ensemble in Step 9 handles
    "no signal" via regime + confidence gating, not via a 3rd model class).
    """
    available_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    working = df[available_cols + [label_col]].copy()
    working = working[working[label_col].isin([1, -1, 1.0, -1.0])]  # binary: drop 0/NaN outcomes (float-safe)
    working = working.dropna()

    X = working[available_cols]
    y = (working[label_col] == 1).astype(int)  # 1 = upper hit (long-favorable), 0 = lower hit
    return X, y, available_cols


def time_ordered_split(X: pd.DataFrame, y: pd.Series, train_ratio: float) -> tuple:
    """Split strictly by row order (time-ordered), never shuffled, to avoid temporal leakage."""
    n = len(X)
    split_idx = int(n * train_ratio)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    return X_train, X_test, y_train, y_test


def train_model(X_train: pd.DataFrame, y_train: pd.Series, cfg: dict):
    """Train raw XGBoost classifier with config-driven hyperparameters/seed."""
    model_cfg = cfg["model"]
    clf = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=model_cfg["random_seed"],
        eval_metric="logloss",
        use_label_encoder=False if hasattr(xgb.XGBClassifier(), "use_label_encoder") else None,
    )
    clf.fit(X_train, y_train)
    return clf


def calibrate_model(base_model, X_train: pd.DataFrame, y_train: pd.Series, cfg: dict):
    """
    Wrap the trained model with probability calibration.
    method comes from config.yaml model.calibration_method ("isotonic" or "sigmoid").
    cv="prefit" would require a held-out calibration set; here we use cv=3 with
    time-respecting behavior approximated by NOT shuffling (sklearn CalibratedClassifierCV
    internally does K-fold, which for small time series windows is an acceptable
    approximation at this training-set scale, but documented as a simplification).
    """
    method = cfg["model"]["calibration_method"]
    calibrated = CalibratedClassifierCV(base_model, method=method, cv=3)
    calibrated.fit(X_train, y_train)
    return calibrated


def save_model(model, feature_cols: list, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    joblib.dump({"model": model, "feature_cols": feature_cols}, path)


def load_model(path: str):
    return joblib.load(path)

