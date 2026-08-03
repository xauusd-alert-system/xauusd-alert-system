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

Now supports multiple model backends:
- xgboost (default)
- random_forest
- lightgbm (optional, if installed)
- ensemble (soft voting of available models)
"""
import os
import logging
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
import joblib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_trainer")


FEATURE_COLUMNS = [
    "ema_9", "ema_21", "ema_50", "ema_200", "rsi", "macd_line", "macd_signal", "macd_hist",
    "atr", "bb_width", "body_ratio", "upper_wick_ratio", "lower_wick_ratio", "candle_direction",
    "adx", "plus_di", "minus_di", "atr_ratio", "mtf_confluence_score",
    "return_1", "return_4", "volume_ratio", "atr_pct",
    "dist_asia_high_atr", "dist_asia_low_atr",
    "garman_klass_vol", "dist_ema50_atr", "dist_ema200_atr", "macd_accel",
    "sin_hour", "cos_hour", "dist_pdh_atr", "dist_pdl_atr",
    "obv", "mfi", "rsi_slope", "volume_zscore",
    "dist_donchian_high_atr", "dist_donchian_low_atr",
    "bb_width_percentile", "atr_percentile",
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


def _get_xgb_classifier(model_cfg: dict, random_state: int):
    """Create XGBoost classifier with conservative regularization."""
    params = {
        "n_estimators": model_cfg.get("n_estimators", 150),
        "max_depth": model_cfg.get("max_depth", 3),
        "learning_rate": model_cfg.get("learning_rate", 0.03),
        "subsample": model_cfg.get("subsample", 0.7),
        "colsample_bytree": model_cfg.get("colsample_bytree", 0.7),
        "reg_alpha": model_cfg.get("reg_alpha", 1.0),
        "reg_lambda": model_cfg.get("reg_lambda", 5.0),
        "random_state": random_state,
        "eval_metric": "logloss",
    }
    if hasattr(xgb.XGBClassifier(), "use_label_encoder"):
        params["use_label_encoder"] = False
    return xgb.XGBClassifier(**params)


def _get_rf_classifier(model_cfg: dict, random_state: int):
    """Create RandomForest classifier."""
    return RandomForestClassifier(
        n_estimators=model_cfg.get("n_estimators_rf", 200),
        max_depth=model_cfg.get("max_depth_rf", 5),
        min_samples_leaf=model_cfg.get("min_samples_leaf_rf", 3),
        random_state=random_state,
        n_jobs=-1,
    )


def _get_lightgbm_classifier(model_cfg: dict, random_state: int):
    """Create LightGBM classifier (if installed)."""
    try:
        import lightgbm as lgb
        return lgb.LGBMClassifier(
            n_estimators=model_cfg.get("n_estimators_lgb", 200),
            max_depth=model_cfg.get("max_depth_lgb", 4),
            learning_rate=model_cfg.get("learning_rate_lgb", 0.05),
            subsample=model_cfg.get("subsample_lgb", 0.8),
            colsample_bytree=model_cfg.get("colsample_bytree_lgb", 0.8),
            random_state=random_state,
            n_jobs=-1,
            verbose=-1,
        )
    except ImportError:
        logger.warning("LightGBM not installed, using XGBoost fallback.")
        return _get_xgb_classifier(model_cfg, random_state)


def _create_ensemble_model(X_train: pd.DataFrame, y_train: pd.Series, cfg: dict):
    """Build a soft-voting ensemble of available classifiers."""
    model_cfg = cfg["model"]
    random_state = model_cfg.get("random_seed", 42)

    estimators = []
    # XGBoost always available
    estimators.append(("xgb", _get_xgb_classifier(model_cfg, random_state)))
    # LightGBM if available
    try:
        import lightgbm as lgb
        estimators.append(("lgb", _get_lightgbm_classifier(model_cfg, random_state)))
    except ImportError:
        pass
    # RandomForest always available
    estimators.append(("rf", _get_rf_classifier(model_cfg, random_state)))

    if not estimators:
        raise ValueError("No classifiers available for ensemble")

    voting = VotingClassifier(
        estimators=estimators,
        voting=model_cfg.get("ensemble_method", "soft_voting") if model_cfg.get("ensemble_method") in ("hard", "soft") else "soft",
    )
    return voting


def train_model(X_train: pd.DataFrame, y_train: pd.Series, cfg: dict):
    """Train a model according to config 'model.type'."""
    model_cfg = cfg["model"]
    random_state = model_cfg.get("random_seed", 42)
    model_type = model_cfg.get("type", "xgboost").lower()

    logger.info(f"Training model type: {model_type}")

    if model_type == "random_forest":
        model = _get_rf_classifier(model_cfg, random_state)
    elif model_type == "lightgbm":
        model = _get_lightgbm_classifier(model_cfg, random_state)
    elif model_type == "ensemble":
        model = _create_ensemble_model(X_train, y_train, cfg)
    else:
        model = _get_xgb_classifier(model_cfg, random_state)

    model.fit(X_train, y_train)
    return model


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