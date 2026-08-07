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

HONEST CALIBRATION (Part B Phase 0+1):
calibrate_model() does NOT use CalibratedClassifierCV's internal shuffled K-fold,
because shuffling destroys temporal ordering and lets the model-fitting rows
overlap the calibrator's validation rows over the label horizon (a real leakage
for time-series ML). Instead the base model is fit strictly on an EARLIER slice
of the training set, a purge gap (>= the labeling horizon) is dropped so no label
window crosses the fit/calibrate boundary, and the calibrator is fit with
cv="prefit" on a strictly LATER trailing held-out set. This mirrors production:
train on old data -> calibrate on the most recent data -> evaluate on the future
(test) window, with no temporal overlap at any boundary.

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
    # Order-flow / microstructure (features/order_flow.py, causal, per-row)
    "cvd", "cvd_slope_10", "order_flow_imbalance_14", "order_flow_imbalance_50",
    "dist_vwap_atr",
]


def build_training_matrix(df: pd.DataFrame, label_col: str = "label", cfg: dict = None) -> tuple:
    """
    Extracts (X, y) from a fully-featured, labeled DataFrame.
    Drops rows with NaN in required feature columns or NaN label (label is NaN
    near the end of the dataset where insufficient future data existed, per
    labeling/label_generator.py - these rows are correctly excluded from training).

    Binary mode (default, model.include_zero_class = false):
    Only directional outcomes are modeled - label 0 ("no clear outcome" inside
    the labeling horizon) is dropped; the encoding matches the training label
    generator, where +1 = long-favorable (upper barrier hit), -1 = short-favorable
    (lower barrier hit). The meta-filter/ensemble in Step 9 handles "no signal"
    via regime + confidence gating, not via a 3rd model class.

    Three-class mode (model.include_zero_class = true, Phase 2):
    label 0 is KEPT as a third class so the model can explicitly learn when
    there is "no edge" (neither barrier hit within the horizon). y is encoded
    {0: short, 1: no_trade, 2: long}. Callers can detect the 3-class encoding by
    checking the returned y for a value of 1 with three classes present, or via
    the `model.include_zero_class` config flag they passed in - ModelPredictor
    reads the same flag at inference time to expose p_short / p_no_trade / p_long.
    """
    model_cfg = (cfg or {}).get("model", {}) if cfg else {}
    include_zero = bool(model_cfg.get("include_zero_class", False))
    use_regime = bool(model_cfg.get("use_regime_feature", False))

    available_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    # Optional feature-subset override (audit action 2: reduce to 12-15
    # features for H1 assets with ~120 events per fold). The subset is saved
    # into the model bundle via available_cols, and ModelPredictor selects
    # exactly its saved feature_cols at inference, so all consumers work
    # unchanged. Unknown names in the subset are ignored with a warning.
    subset = model_cfg.get("feature_subset")
    if subset:
        unknown = [f for f in subset if f not in available_cols]
        if unknown:
            print(f"[trainer] WARNING: feature_subset entries not available, "
                  f"ignored: {unknown}")
        available_cols = [f for f in subset if f in available_cols] or available_cols
    if use_regime:
        # Phase 3: expand the causal `regime` column (already computed by
        # classify_regime_series upstream) into one-hot columns and append them to
        # the feature set. These columns are saved into the model bundle via
        # available_cols, and ModelPredictor re-synthesizes them at inference time
        # from the raw `regime` column, so every consumer works unchanged.
        if "regime" not in df.columns:
            raise ValueError(
                "model.use_regime_feature=true requires a 'regime' column on the "
                "DataFrame (compute it with classify_regime_series first)"
            )
        from regime.classifier import regime_onehot_df
        onehot = regime_onehot_df(df)
        new_regime_cols = [c for c in onehot.columns if c not in available_cols]
        available_cols = available_cols + new_regime_cols
        # Merge the one-hot columns into the working frame so the section below can
        # slice available_cols + label_col off one DataFrame (the caller's df is
        # left untouched - concat returns a new object, so this is side-effect free).
        df = pd.concat([df, onehot[new_regime_cols]], axis=1)

    working = df[available_cols + [label_col]].copy()

    if include_zero:
        # Keep label 0 as a third class: {0: short, 1: no_trade, 2: long}.
        working = working[working[label_col].isin([1, -1, 0, 1.0, -1.0, 0.0])]
        working = working.dropna()
        y_map = {1: 2, -1: 0}  # long-favorable -> 2, short-favorable -> 0
        y = working[label_col].map(lambda v: y_map.get(int(v), 1))  # 0 (or any other) -> 1 = no_trade
    else:
        # Binary: drop 0 outcomes (float-safe), keep only +/- 1.
        working = working[working[label_col].isin([1, -1, 1.0, -1.0])]
        working = working.dropna()
        y = (working[label_col] == 1).astype(int)  # 1 = upper hit (long-favorable), 0 = lower hit

    X = working[available_cols]

    # Class codes: binary -> {0, 1}; three-class -> {0: short, 1: no_trade, 2: long}.
    y = y.astype(int)
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


def train_model(X_train: pd.DataFrame, y_train: pd.Series, cfg: dict,
                sample_weight: np.ndarray | None = None):
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

    if sample_weight is not None:
        sw = np.asarray(sample_weight, dtype=float)
        if len(sw) != len(y_train):
            raise ValueError(
                f"sample_weight length {len(sw)} != y_train length {len(y_train)}")
        # XGBoost (and RF/LGBM) accept sample_weight in fit.
        model.fit(X_train, y_train, sample_weight=sw)
    else:
        model.fit(X_train, y_train)
    return model


def calibrate_model(base_model, X_train: pd.DataFrame, y_train: pd.Series, cfg: dict):
    """
    Wrap the trained model with HONEST probability calibration using a purged,
    time-ordered split -- no shuffled K-fold, no temporal overlap (Phase 0+1).

    The caller's base_model is treated as a pre-fit template/structure for its
    hyper-parameters. We build the SAME model class, REFIT it strictly on an EARLIER
    slice of X_train, drop a purge gap of `labeling.horizon_candles_n` rows so no
    label window crosses the fit/calibrate boundary, then fit the calibrator on the
    strictly LATER (held-out) slice using an explicit single time-ordered split as
    the `cv` argument (never sklearn's internal shuffled K-fold).

    This mirrors production semantics: train on old data -> calibrate on the most
    recent data -> be evaluated on future (out-of-sample) data. It removes the former
    CalibratedClassifierCV(cv=3) internal shuffled K-fold, which let rows used to fit
    the model leak into the row set used to fit the calibrator across the label
    horizon -- a real time-series leakage bug that inflated honest-validation metrics.

    Fallback: if the training set is too small for a valid purged split (fewer than
    ~2 * purge_gap + min_calibration_fit + min_calibration_rows rows, or either final
    slice ends up class-imbalanced), we degrade to a NON-leaky small calibration fit:
    the base model is still trained on the FULL set, and the calibration is applied as
    a no-op identity wrapper so probabilities stay raw (never silently accepted from a
    shuffled K-fold). Callers check this via the returned object's
    `_is_honest_placeholder` attribute.
    """
    method = cfg["model"]["calibration_method"]
    horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 24))
    # Calibration needs a modest number of rows to be stable; keep it conservative so
    # short training sets in tests/backtests still function.
    min_fit = max(30, min(200, len(X_train) // 3 // 2))  # ~ 1/6 of data, lower-bounded
    min_calib = max(20, min(100, len(X_train) // 6 // 2))  # ~ 1/12 of data, lower-bounded

    n = len(X_train)
    if n < min_fit + horizon + min_calib + 1:
        # Too small for a valid purged split: rebuild base on the full set, no-op calibration.
        logger.warning(
            "calibrate_model: insufficient data for a purged calibration split "
            "(n=%d < fit=%d + purge=%d + calib=%d); applying identity calibration.",
            n, min_fit, horizon, min_calib,
        )
        base = train_model(X_train, y_train, cfg)  # deterministic refit, same seed
        _attach_noop_calibration(base, method_invalid=True)
        return base

    # Strictly time-ordered split: EARLIER slice fits the model, LATER slice fits the calibrator.
    fit_end = n - horizon - min_calib                # last row index usable for the model fit
    if fit_end < min_fit:
        fit_end = min_fit
    X_fit = X_train.iloc[:fit_end]
    y_fit = y_train.iloc[:fit_end]
    X_calib = X_train.iloc[fit_end + horizon:]       # purge gap of `horizon` rows in between
    y_calib = y_train.iloc[fit_end + horizon:]

    if len(X_fit) < 2 or y_fit.nunique() < 2:
        logger.warning("calibrate_model: calibration-fit slice is degenerate; identity calibration.")
        base = train_model(X_train, y_train, cfg)  # refit on full set (deterministic seed)
        _attach_noop_calibration(base, method_invalid=True)
        return base
    if len(X_calib) < 5 or y_calib.nunique() < 2:
        logger.warning("calibrate_model: calibration held-out slice lacks class diversity; identity calibration.")
        base = train_model(X_train, y_train, cfg)
        _attach_noop_calibration(base, method_invalid=True)
        return base

    logger.info(
        "calibrate_model: purged split fit_rows=%d calib_rows=%d purge_gap=%d",
        len(X_fit), len(X_calib), horizon,
    )

    # Fit the base model on the earlier slice, then calibrate on the strictly LATER
    # trailing slice. Using an explicit single time-ordered split as cv avoids
    # sklearn's internal shuffled K-fold entirely (cv=3 would re-shuffle and let
    # model-fit rows overlap calibrator rows across the label horizon -- leakage).
    # base_model acts as the hyper-parameter template; sklearn clones + refits it on
    # exactly the EARLIER fit indices and fits the calibrator on exactly the LATER,
    # purged test indices (positions in X_train).
    split_indices = [
        (np.arange(fit_end), np.arange(fit_end + horizon, n))
    ]
    calibrated = CalibratedClassifierCV(base_model, method=method, cv=split_indices)
    calibrated.fit(X_train, y_train)
    return calibrated


def _attach_noop_calibration(model, method_invalid: bool) -> None:
    """
    Mark a model as having NO real calibration applied (degraded path / placeholder).

    ModelPredictor must treat a model without calibrated fitted coefficients as raw
    probabilities. We attach minimal attributes so callers can detect the placeholder.
    """
    model._is_honest_placeholder = True
    model._calibration_method_applied = None
    if method_invalid:
        model._calibration_skipped_reason = "insufficient_purged_data"


def save_model(model, feature_cols: list, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    joblib.dump({"model": model, "feature_cols": feature_cols}, path)


def load_model(path: str):
    return joblib.load(path)