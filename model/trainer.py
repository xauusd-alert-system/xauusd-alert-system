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
test rows, which is a subtle but real leakage risk in time series ML. Time ordering
alone is not sufficient, however: see purged_time_ordered_split below for why the
rows adjacent to the split boundary must also be dropped.

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
import logging
import os

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier, VotingClassifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_trainer")

# A walk-forward run can hit the same data-quality fallback in every fold. Keep
# its logs actionable instead of printing an identical warning dozens of times.
_WARNED_PREFIXES: set[str] = set()


def _warn_once(prefix: str, message: str, *args) -> None:
    """Emit a warning at most once per process for a logical warning family."""
    if prefix in _WARNED_PREFIXES:
        return
    _WARNED_PREFIXES.add(prefix)
    logger.warning(message, *args)


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
    # Agent-based bifurcation (features/bifurcation.py, causal entropy)
    "break_score", "break_intensity", "agent_long_ratio",
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


class DegenerateLabelSpaceError(ValueError):
    """
    Raised when the observed training labels cannot yield a model whose
    probability columns ModelPredictor is able to decode into p_short / p_long.

    This is a DATA condition (a training window that simply does not contain a
    given outcome), not a code defect, so callers that iterate over many windows
    (walk-forward backtests) are expected to catch it and treat that window as
    "no signal" instead of aborting the whole run.
    """


# Semantic class codes emitted by build_training_matrix in three-class mode.
_CLS_SHORT = 0
_CLS_NO_TRADE = 1
_CLS_LONG = 2


def _normalize_label_space(y: pd.Series, cfg: dict = None) -> tuple:
    """
    Map the semantic label space onto a CONTIGUOUS, decodable one.

    Why this exists
    ---------------
    build_training_matrix encodes the three-class model as {0: short,
    1: no_trade, 2: long}. XGBoost's scikit-learn wrapper requires the observed
    class labels to be exactly [0, 1, ..., n_classes-1] and infers n_classes
    from `np.unique(y)` of the data it is handed, so a training window that
    contains no `no_trade` rows arrives as classes [0, 2] and fit() dies with

        ValueError: Invalid classes inferred from unique values of `y`.
                    Expected: [0 1], got [0 2]

    That killed the entire GBPUSD walk-forward run at the first such fold (the
    three-class asset). The triple-barrier labeller only emits label 0 when
    NEITHER barrier is touched inside the horizon, which on H1/36-bar settings
    is rare, so this window is common rather than exotic.

    Mapping
    -------
    Three-class mode ({0: short, 1: no_trade, 2: long}):
      * {0, 1, 2} -> unchanged. ModelPredictor sees class 2 and decodes
        p_short / p_no_trade / p_long.
      * {0, 2}    -> remapped {0 -> 0, 2 -> 1}. The no_trade class was never
        observed, so the honest model is a BINARY short/long one; the remap is
        exactly the binary encoding ModelPredictor already decodes as
        p_short = P(class 0), p_long = P(class 1). No no_trade mass exists,
        which is truthful: no such example was in the training window.
      * {0, 1} / {1, 2} -> refused. Here an entire trade DIRECTION is missing
        while no_trade is present. Such a fit produces two columns whose second
        one is P(no_trade), which ModelPredictor's binary branch would decode as
        p_long (or p_short) -- a silently WRONG directional probability. Better
        to raise and let the caller degrade the window explicitly.

    Binary mode ({0: short, 1: long}) is already contiguous and passes through.

    Returns (y_normalized, info) where info carries observability flags.
    """
    model_cfg = (cfg or {}).get("model", {}) if cfg else {}
    include_zero = bool(model_cfg.get("include_zero_class", False))

    y = pd.Series(y)
    observed = {int(v) for v in pd.unique(y.dropna())}
    info = {
        "include_zero_class": include_zero,
        "observed_classes": sorted(observed),
        "no_trade_class_absent": False,
        "remapped": False,
    }

    if len(observed) < 2:
        raise DegenerateLabelSpaceError(
            f"training labels contain a single class {sorted(observed)}; a "
            "directional model needs at least two classes"
        )

    if not include_zero:
        # Binary encoding is {0: short, 1: long} -- note that here 1 means LONG,
        # not no_trade, so the three-class constants deliberately are not reused.
        if observed <= {0, 1}:
            return y.astype(int), info
        raise DegenerateLabelSpaceError(
            f"binary training labels must be a subset of {{0, 1}}, got "
            f"{sorted(observed)}"
        )

    if observed == {_CLS_SHORT, _CLS_NO_TRADE, _CLS_LONG}:
        return y.astype(int), info

    if observed == {_CLS_SHORT, _CLS_LONG}:
        # run_backtest normally detects this condition before building y and
        # makes the fold binary. Keep the shared training entry point safe for
        # production callers too, but do not spam this expected fold-local path.
        if not model_cfg.get("include_zero_class_effectively_binary", False):
            _warn_once(
                "normalize_label_space:no_trade_absent",
                "normalize_label_space: include_zero_class=true but this training "
                "window has NO no_trade rows (classes [0, 2]); remapping to a "
                "binary short/long model {0->0, 2->1}. XGBoost cannot fit "
                "non-contiguous classes, and p_no_trade would be meaningless here.",
            )
        info["no_trade_class_absent"] = True
        info["remapped"] = True
        return y.astype(int).map({_CLS_SHORT: 0, _CLS_LONG: 1}).astype(int), info

    if observed in ({_CLS_SHORT, _CLS_NO_TRADE}, {_CLS_NO_TRADE, _CLS_LONG}):
        missing = "long (2)" if observed == {_CLS_SHORT, _CLS_NO_TRADE} else "short (0)"
        raise DegenerateLabelSpaceError(
            f"three-class training labels {sorted(observed)} keep the no_trade "
            f"class (1) but contain no {missing} outcomes, so the fitted model "
            "cannot express both trade directions and its second probability "
            "column would be decoded as the wrong side; refusing to train"
        )

    raise DegenerateLabelSpaceError(
        f"three-class training labels must be a subset of {{0, 1, 2}}, got "
        f"{sorted(observed)}"
    )


def normalize_label_space(y: pd.Series, cfg: dict = None) -> pd.Series:
    """
    Public wrapper around _normalize_label_space returning only the labels.
    See _normalize_label_space for the full rationale and mapping table.
    """
    return _normalize_label_space(y, cfg)[0]


def time_ordered_split(X: pd.DataFrame, y: pd.Series, train_ratio: float) -> tuple:
    """Split strictly by row order (time-ordered), never shuffled, to avoid temporal leakage."""
    n = len(X)
    split_idx = int(n * train_ratio)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    return X_train, X_test, y_train, y_test


def purged_time_ordered_split(
    X: pd.DataFrame,
    y: pd.Series,
    train_ratio: float,
    horizon: int = 0,
    embargo: int = 0,
) -> tuple:
    """
    Time-ordered split that ALSO drops the training rows whose labels reach into
    the test set.

    time_ordered_split above is causally ordered but not causally clean. Under
    triple-barrier labelling, row i's label is decided by bars i+1 .. i+horizon,
    so the last `horizon` rows of the training slice were labelled by outcomes
    that occur inside the test window: the model is scored on bars it has
    already been shown the answer for. `embargo` drops a further block on top,
    because rolling features carry state across the boundary - obv,
    atr_percentile and bb_width_percentile each look back ~100 bars, so a test
    row near the split shares most of its window with the last training rows.

    Only the TRAINING side shrinks. The test slice is exactly what
    time_ordered_split returns for the same train_ratio, which is what makes
    before/after metrics comparable rather than merely similar.

    backtest/walk_forward.run_walk_forward has always purged this way; this
    function applies the same rule to the path that produces the model that is
    actually deployed, so the two stop disagreeing about what "out of sample"
    means.

    horizon: labeling.horizon_candles_n
    embargo: backtest.walk_forward.embargo_candles
    """
    n = len(X)
    split_idx = int(n * train_ratio)
    gap = max(0, int(horizon)) + max(0, int(embargo))
    train_end = max(0, split_idx - gap)

    if train_end == 0 and split_idx > 0:
        _warn_once(
            "purged_time_ordered_split:gap_ate_training_set",
            "purged_time_ordered_split: purge gap (%d rows) is not smaller than "
            "the training slice (%d rows), so NO training data survives. Reduce "
            "backtest.walk_forward.embargo_candles or supply more history.",
            gap, split_idx,
        )

    X_train, X_test = X.iloc[:train_end], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:train_end], y.iloc[split_idx:]
    return X_train, X_test, y_train, y_test


class LogitMarginEstimator(BaseEstimator, ClassifierMixin):
    """Expose a binary classifier's log-odds margin as ``decision_function``.

    Canonical Platt scaling fits the sigmoid ``1/(1+exp(a*x+b))`` on the
    log-odds margin ``logit(p) = ln(p/(1-p))``. XGBoost exposes no
    ``decision_function``, so sklearn's ``_SigmoidCalibration`` was historically
    fed raw probabilities (already squashed near 0.5), which collapsed the
    calibrated spread and killed the minority direction (audit 2026-08-25).
    Wrapping the base classifier with this estimator restores the canonical
    input space.

    IMPORTANT (artifact compatibility): production joblib bundles pickle this
    class by reference (module path ``model.trainer.LogitMarginEstimator``);
    removing or renaming it breaks unpickling of every previously saved model.
    """

    def __init__(self, estimator=None, base_estimator=None):
        # ``estimator`` follows the sklearn convention CalibratedClassifierCV
        # relies on when cloning (production pickles store the base classifier
        # under this attribute). ``base_estimator`` kept as an alias.
        self.estimator = base_estimator if base_estimator is not None else estimator

    @property
    def base_estimator(self):
        return self.estimator_

    def fit(self, X, y, **kwargs):
        self.estimator.fit(X, y, **kwargs)
        self.estimator_ = self.estimator  # convention: trailing _ = fitted
        return self

    @property
    def classes_(self):
        if hasattr(self, "classes_ref") and self.classes_ref is not None:
            return self.classes_ref
        est = getattr(self, "estimator_", None) or self.estimator
        return est.classes_

    def _margin(self, proba):
        # proba column for the positive class; guard against 0/1 saturation.
        p = np.clip(np.asarray(proba, dtype=float), 1e-6, 1 - 1e-6)
        return np.log(p / (1 - p))

    def decision_function(self, X):
        """Return the raw log-odds margin of the positive class."""
        proba = self._inner().predict_proba(X)
        # Binary case: sklearn expects a 1-D margin; take the positive class.
        return self._margin(proba[:, 1])

    def predict_proba(self, X):
        return self._inner().predict_proba(X)

    def predict(self, X):
        return self._inner().predict(X)

    def _inner(self):
        """Fitted estimator if present, otherwise the unfitted template."""
        return getattr(self, "estimator_", None) or self.estimator


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


def _fit_classifier(X_train: pd.DataFrame, y_train: pd.Series, cfg: dict,
                    sample_weight: np.ndarray | None = None):
    """
    Build and fit the configured classifier on an ALREADY label-normalized y.

    Kept separate from train_model so calibrate_model can refit internally
    without running _normalize_label_space a second time: the mapping is not
    idempotent by value (a normalized binary {0, 1} is indistinguishable from a
    three-class window holding only {short, no_trade}), so it must be applied
    exactly once per training set.
    """
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


def train_model(X_train: pd.DataFrame, y_train: pd.Series, cfg: dict,
                sample_weight: np.ndarray | None = None):
    """
    Train a model according to config 'model.type'.

    The label space is normalized first (see _normalize_label_space): XGBoost
    refuses non-contiguous classes, so a three-class training window that holds
    no `no_trade` rows is fitted as a binary short/long model instead of
    aborting. Windows where an entire direction is missing raise
    DegenerateLabelSpaceError so the caller can skip them explicitly.
    """
    y_train, label_info = _normalize_label_space(y_train, cfg)
    model = _fit_classifier(X_train, y_train, cfg, sample_weight=sample_weight)
    # Observability, mirroring the _is_honest_placeholder convention: record when
    # a three-class asset was actually fitted as binary for lack of no_trade rows.
    model._label_space_no_trade_absent = bool(label_info["no_trade_class_absent"])
    return model


def calibrate_model(base_model, X_train: pd.DataFrame, y_train: pd.Series, cfg: dict,
                    sample_weight: np.ndarray | None = None):
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

    ``sample_weight`` is the same index-aligned uniqueness vector used for the
    production base fit.  It is supplied to CalibratedClassifierCV so both its
    cloned estimator fit and held-out calibrator fit obey one explicit weighting
    policy; fallback refits use it as well.
    """
    method = cfg["model"]["calibration_method"]
    sw = None
    if sample_weight is not None:
        sw = np.asarray(sample_weight, dtype=float)
        if len(sw) != len(y_train):
            raise ValueError(
                f"sample_weight length {len(sw)} != y_train length {len(y_train)}"
            )
        if not np.isfinite(sw).all() or (sw < 0).any() or not (sw > 0).any():
            raise ValueError("sample_weight must be finite, non-negative and not all zero")
    # Normalize ONCE here (never again downstream): CalibratedClassifierCV refits
    # the estimator on raw label slices of this y, so the labels it forwards to
    # XGBoost must already be contiguous. Internal refits below therefore use
    # _fit_classifier, not train_model.
    y_train, _label_info = _normalize_label_space(y_train, cfg)
    horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 24))
    # Slice sizes are PROPORTIONAL, never capped. The previous form was
    #     min_fit   = max(30, min(200, len(X_train) // 3 // 2))
    #     min_calib = max(20, min(100, len(X_train) // 6 // 2))
    # and those min(200, ...) / min(100, ...) ceilings meant that every training
    # set larger than ~1200 rows calibrated on exactly 100 rows regardless of how
    # much history it had. On the real XAUUSD M15 set that was 100 rows out of
    # 48738 (0.2%): the sigmoid fitted on it collapsed the probability spread,
    # coverage at p>=0.55 reached 98% and the short side stopped firing at every
    # configured threshold. Shares preserve the original intent (an earlier fit
    # slice, a trailing held-out slice) while letting the calibrator scale with
    # the data. The lower bounds keep short test/backtest windows working.
    _cal_cfg = cfg.get("model", {})
    _fit_share = float(_cal_cfg.get("calibration_fit_min_share", 0.60))
    _holdout_share = float(_cal_cfg.get("calibration_holdout_share", 0.15))
    min_fit = max(30, int(len(X_train) * _fit_share))
    min_calib = max(20, int(len(X_train) * _holdout_share))

    n = len(X_train)
    if n < min_fit + horizon + min_calib + 1:
        # Too small for a valid purged split: rebuild base on the full set, no-op calibration.
        _warn_once(
            "calibrate_model:insufficient_purged_data",
            "calibrate_model: insufficient data for a purged calibration split "
            "(n=%d < fit=%d + purge=%d + calib=%d); applying identity calibration.",
            n, min_fit, horizon, min_calib,
        )
        base = _fit_classifier(X_train, y_train, cfg, sample_weight=sw)  # deterministic refit, same seed
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

    # Both slices must carry the FULL class set, not merely "2+ classes". A slice
    # that drops one class of a three-class window would (a) hand XGBoost the same
    # non-contiguous labels that crash fit() and (b) leave CalibratedClassifierCV
    # fitting a per-class sigmoid against a probability column that does not exist.
    # For the binary space this is equivalent to the previous nunique() < 2 check,
    # so the two-class path behaves exactly as before.
    full_classes = set(y_train.unique())
    if len(X_fit) < 2 or set(y_fit.unique()) != full_classes:
        _warn_once(
            "calibrate_model:degenerate_fit_slice",
            "calibrate_model: calibration-fit slice is degenerate "
            "(classes %s vs full %s); identity calibration.",
            sorted(set(y_fit.unique())), sorted(full_classes),
        )
        base = _fit_classifier(X_train, y_train, cfg, sample_weight=sw)  # refit on full set (deterministic seed)
        _attach_noop_calibration(base, method_invalid=True)
        return base
    if len(X_calib) < 5 or set(y_calib.unique()) != full_classes:
        _warn_once(
            "calibrate_model:heldout_slice_lacks_diversity",
            "calibrate_model: calibration held-out slice lacks class diversity "
            "(classes %s vs full %s); identity calibration.",
            sorted(set(y_calib.unique())), sorted(full_classes),
        )
        base = _fit_classifier(X_train, y_train, cfg, sample_weight=sw)
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
    fit_kwargs = {"sample_weight": sw} if sw is not None else {}
    calibrated.fit(X_train, y_train, **fit_kwargs)
    calibrated._calibration_weight_mode = (
        "sample_weight" if sw is not None else "unweighted"
    )
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


def compute_model_fingerprint(model, feature_cols: list) -> str:
    """Deterministic content fingerprint of a trained artifact.

    joblib serialization is NOT reproducible (two dumps of the same object can
    differ), so the fingerprint is built from deterministic pieces instead:

      * the XGBoost booster's raw model buffer (``save_raw``) + config
        (``save_config``) — the actual tree weights/parameters;
      * the canonical JSON of ``feature_cols``;
      * the fitted calibration coefficients (sigmoid a_/b_, or isotonic bins)
        when the wrapper exposes them (``CalibratedClassifierCV``).

    Works for a raw ``XGBClassifier`` (or anything with ``get_booster``) and for
    a ``CalibratedClassifierCV``/``VotingClassifier`` wrapper that exposes the
    booster somewhere down the attribute tree. Two artifacts trained with the
    same weights, features and calibration always hash equal, so the value
    stored in ``metadata.model_hash`` can be recomputed after loading to verify
    the file actually contains what it claims.
    """
    import hashlib
    import json

    def _find_booster(obj):
        seen = set()
        stack = [obj]
        while stack:
            cur = stack.pop()
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            get_booster = getattr(cur, "get_booster", None)
            if callable(get_booster):
                try:
                    return get_booster()
                except Exception:
                    pass
            for attr in ("estimator", "calibrated_classifiers_", "estimators_", "base_estimator"):
                child = getattr(cur, attr, None)
                if isinstance(child, (list, tuple)):
                    stack.extend(child)
                elif child is not None:
                    stack.append(child)
        return None

    parts: list[bytes] = []
    booster = _find_booster(model)
    if booster is not None:
        parts.append(bytes(booster.save_raw()))
        parts.append(str(booster.save_config()).encode("utf-8"))
    else:
        # Non-XGBoost fallback: type name + repr is deterministic for the same
        # fitted object in the same environment (calibrators included).
        parts.append(repr(model).encode("utf-8"))

    parts.append(json.dumps(list(feature_cols), sort_keys=True).encode("utf-8"))

    calibrators = getattr(model, "calibrated_classifiers_", None)
    if calibrators:
        for cc in calibrators:
            for cal in getattr(cc, "calibrators", []) or []:
                for key in ("a_", "b_"):
                    val = getattr(cal, key, None)
                    if val is not None:
                        parts.append(f"{key}={val!r}".encode())

    return hashlib.sha256(b"\x00".join(parts)).hexdigest()


def save_model(model, feature_cols: list, path: str, metadata: dict | None = None):
    """Persist a model bundle with an auditable, backward-compatible contract.

    When ``metadata`` is provided and does not already carry a ``model_hash``,
    the artifact's deterministic content fingerprint (see
    ``compute_model_fingerprint``) is injected so the model carries its own hash
    inside itself — loading the file reveals its identity without recomputing.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if metadata is not None and not metadata.get("model_hash"):
        metadata = {**metadata, "model_hash": compute_model_fingerprint(model, feature_cols)}
    bundle = {"model": model, "feature_cols": feature_cols}
    if metadata is not None:
        bundle["metadata"] = metadata
    joblib.dump(bundle, path)


def load_model(path: str):
    return joblib.load(path)
