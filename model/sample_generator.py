"""Sample generator for XAUUSD following the book's create_initial_data.mq5
pattern (NN book pages 222-229; TZ_BOOKS task T-02).

Book pattern transferred to the Python pipeline:

1. **Features** (book p. 87-99, 12): RSI + MACD + candle geometry
   (upper wick / body / lower wick, each normalized by the bar range so the
   geometry is scale-free like the book's perceptron diagram on p. 12).
2. **Normalization with SAVED parameters**: z-score (or min-max) parameters
   are fitted on the TRAIN split only and serialized to JSON, so the exact
   same parameters are applied in live mode - the book's warning (p. 223)
   that unsaved normalization parameters make train/live distributions
   incompatible is the bug class this kills.
3. **Time-ordered 60/20/20 split**: train/valid/test are consecutive time
   slices (no shuffling across the boundary; book p. 222, 255).
4. **Targets**: the future close-to-close return over ``horizon`` bars,
   z-scored with train-only parameters - the regression target of the
   book's "direction and strength" output (2 neurons by default, i.e. the
   same horizon return fed to both output slots is NOT used; instead the
   generator can emit multi-horizon targets).

Causality: every feature at bar t uses data up to and including bar t; the
target uses close[t + horizon] and therefore exists only for the offline
training path (never in the inference pipeline), mirroring the repo-wide
no-lookahead invariant with labeling quarantined from inference.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger("book_sample_generator")

DEFAULT_CFG: dict = {
    "rsi_period": 14,
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "candle_geometry": True,
    "extended": False,        # T-19: ATR, session volatility, volume
    "atr_period": 14,
    "session_vol_window": 96,  # bars for the rolling session-vol proxy
    "horizon": 12,            # bars ahead for the target
    "window": 16,             # input sequence length (bars per sample)
    "normalization": "zscore",  # zscore | minmax | none
    "split": {"train": 0.6, "valid": 0.2, "test": 0.2},
    "target_mode": "return",  # return | multi_horizon (2 outputs, book style)
    "multi_horizons": [6, 12],
}


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50.0)


def _macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    line = _ema(close, fast) - _ema(close, slow)
    sig = _ema(line, signal)
    return pd.DataFrame({"macd_line": line, "macd_signal": sig, "macd_hist": line - sig})


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    # min_periods=1 (was = period): the head NaNs poisoned the first
    # windowed samples on real data (13 non-finite atr_ratio bars -> NaN
    # training loss with --extended-features). Partial-window EMA is still
    # strictly causal; synthetic data never surfaced this.
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()


FEATURE_COLUMNS_BASE = [
    "rsi", "macd_line", "macd_signal", "macd_hist",
    "upper_wick", "body", "lower_wick",
]
FEATURE_COLUMNS_EXTENDED = FEATURE_COLUMNS_BASE + [
    "atr_ratio", "session_vol_ratio", "volume_ratio",
]


def build_book_features(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Causal feature frame for the book feature set (RSI+MACD+geometry).

    ``df`` needs open/high/low/close and (for the extended set) volume,
    indexed or ordered by time ascending. All features at row t use bars
    <= t only.
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    out = pd.DataFrame(index=df.index)
    close = df["close"].astype(float)
    out["rsi"] = _rsi(close, int(cfg["rsi_period"]))
    macd = _macd(close, int(cfg["macd"]["fast"]), int(cfg["macd"]["slow"]),
                 int(cfg["macd"]["signal"]))
    for col in ("macd_line", "macd_signal", "macd_hist"):
        out[col] = macd[col]
    if cfg["candle_geometry"]:
        rng_ = (df["high"] - df["low"]).replace(0, np.nan)
        out["upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / rng_
        out["body"] = (df["close"] - df["open"]) / rng_
        out["lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / rng_
        out[["upper_wick", "body", "lower_wick"]] = \
            out[["upper_wick", "body", "lower_wick"]].fillna(0.0)
    if cfg["extended"]:  # T-19
        out["atr_ratio"] = _atr(df, int(cfg["atr_period"])) / close
        # rolling volatility proxy of the session (causal): stdev of the
        # last `session_vol_window` close-to-close returns. Real-data
        # bugfix: the naive std/mean(|ret|) coefficient of variation
        # explodes (observed 4.2e6 on dead-flat 32-bar windows of the
        # 2004-2025 XAUUSD history) and poisons z-scored training. The
        # denominator is floored by 5% of the same window's std, bounding
        # the ratio at 20 for vanishing mean moves; still scale-free.
        ret = close.pct_change()
        ret_std = ret.rolling(int(cfg["session_vol_window"]),
                              min_periods=2).std()
        ret_mean_abs = ret.rolling(int(cfg["session_vol_window"]),
                                   min_periods=2).mean().abs()
        out["session_vol_ratio"] = (ret_std
                                    / (ret_mean_abs + 0.05 * ret_std
                                       + 1e-12)).fillna(0.0).clip(upper=20.0)
        if "volume" in df.columns:
            vol_mean = df["volume"].rolling(int(cfg["session_vol_window"]),
                                            min_periods=1).mean()
            out["volume_ratio"] = (df["volume"] / vol_mean.replace(0, np.nan)).fillna(1.0)
        else:
            out["volume_ratio"] = 1.0
    return out


def build_target(df: pd.DataFrame, horizon: int) -> pd.Series:
    """Future close-to-close return over ``horizon`` bars (offline label)."""
    close = df["close"].astype(float)
    return close.shift(-horizon) / close - 1.0


def build_multi_horizon_target(df: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    """Book-style 2-neuron output target: future returns over several
    horizons (one column each), z-scored downstream with train stats."""
    close = df["close"].astype(float)
    out = {}
    for h in horizons:
        out[f"ret_{h}"] = close.shift(-int(h)) / close - 1.0
    return pd.DataFrame(out)


def make_windowed_samples(features: pd.DataFrame, target: pd.Series | pd.DataFrame,
                          window: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sliding-window samples: X (N, window, D), y (N,) or (N, H), bar_index (N,).

    A sample starting at bar i uses feature rows [i, i+window) and the
    target at bar i+window-1 (the horizon return measured from the last
    feature bar's close) - strictly causal, no overlap with the future
    beyond the label itself. Rows with any non-finite target entry are
    dropped (the label horizon eats the frame tail).
    """
    vals = features.to_numpy(dtype=float)
    tgt = np.asarray(target, dtype=float)
    if tgt.ndim == 1:
        tgt = tgt[:, None]
    n = len(vals)
    xs, ys, idxs = [], [], []
    for i in range(0, n - window + 1):
        last = i + window - 1
        row = tgt[last]
        if not np.all(np.isfinite(row)):
            continue
        xs.append(vals[i:i + window])
        ys.append(row if row.shape[0] > 1 else float(row[0]))
        idxs.append(last)
    if not xs:
        y_cols = tgt.shape[1] if tgt.ndim == 2 else 1
        return (np.empty((0, window, vals.shape[1] if vals.ndim == 2 else 0)),
                np.empty((0, y_cols)), np.empty(0, dtype=int))
    return np.array(xs), np.array(ys), np.array(idxs, dtype=int)


@dataclass
class NormalizationParams:
    """Serialized normalization parameters (the T-02 core requirement)."""

    method: str
    columns: list[str]
    center: dict[str, float] = field(default_factory=dict)
    scale: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"method": self.method, "columns": self.columns,
                "center": self.center, "scale": self.scale}

    @classmethod
    def from_dict(cls, d: dict) -> "NormalizationParams":
        return cls(method=d["method"], columns=list(d["columns"]),
                   center=dict(d.get("center", {})), scale=dict(d.get("scale", {})))


def fit_normalization(features: pd.DataFrame, method: str = "zscore") -> NormalizationParams:
    """Fit normalization on the TRAIN feature frame only."""
    if method not in ("zscore", "minmax", "none"):
        raise ValueError(f"unknown normalization method {method!r}")
    cols = list(features.columns)
    if method == "none":
        return NormalizationParams(method="none", columns=cols)
    center, scale = {}, {}
    for col in cols:
        s = features[col].astype(float)
        if method == "zscore":
            mu = float(s.mean())
            sd = float(s.std(ddof=0))
            center[col], scale[col] = mu, sd if sd > 1e-12 else 1.0
        else:  # minmax -> [0, 1]
            lo, hi = float(s.min()), float(s.max())
            center[col] = lo
            scale[col] = (hi - lo) if (hi - lo) > 1e-12 else 1.0
    return NormalizationParams(method=method, columns=cols, center=center, scale=scale)


def apply_normalization(features: pd.DataFrame, params: NormalizationParams) -> pd.DataFrame:
    """Apply TRAIN-fitted parameters to any frame (valid/test/live)."""
    if params.method == "none":
        return features.copy()
    out = features.copy()
    for col in params.columns:
        if col not in out.columns:
            raise KeyError(f"normalization parameter for missing column {col!r}")
        if params.method == "zscore":
            out[col] = (out[col] - params.center[col]) / params.scale[col]
        else:
            out[col] = (out[col] - params.center[col]) / params.scale[col]
    return out


def save_normalization_params(params: NormalizationParams, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(params.to_dict(), fh, indent=2)
    return path


def load_normalization_params(path: str) -> NormalizationParams:
    with open(path, "r", encoding="utf-8") as fh:
        return NormalizationParams.from_dict(json.load(fh))


def split_indices_time_ordered(n: int, ratios: tuple[float, float, float]) -> tuple[int, int]:
    """Consecutive time-ordered split boundaries (train/valid/test)."""
    if len(ratios) != 3 or any(r < 0 for r in ratios):
        raise ValueError(f"need three non-negative ratios, got {ratios}")
    total = float(sum(ratios))
    if total <= 0:
        raise ValueError("ratios must not sum to 0")
    train_n = int(round(n * ratios[0] / total))
    valid_n = int(round(n * ratios[1] / total))
    if train_n + valid_n >= n:
        valid_n = max(0, n - train_n - 1)
    return train_n, train_n + valid_n


@dataclass
class BookSamples:
    X_train: np.ndarray
    y_train: np.ndarray
    X_valid: np.ndarray
    y_valid: np.ndarray
    X_test: np.ndarray
    y_test: np.ndarray
    norm_params: NormalizationParams
    feature_columns: list[str]
    target_scale: float | np.ndarray  # train std of the raw target(s)

    def split_sizes(self) -> dict[str, int]:
        return {"train": len(self.X_train), "valid": len(self.X_valid),
                "test": len(self.X_test)}


def generate_book_samples(df: pd.DataFrame, cfg: dict | None = None,
                          norm_params_path: str | None = None) -> BookSamples:
    """Full create_initial_data pattern: features -> normalize (train-only,
    parameters saved) -> time-ordered 60/20/20 split.

    ``norm_params_path``: when given, the fitted TRAIN parameters are written
    there (JSON) for the live EA / inference path to load verbatim.
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    if len(df) < cfg["window"] + cfg["horizon"] + 10:
        raise ValueError(f"not enough bars ({len(df)}) for window={cfg['window']} "
                         f"and horizon={cfg['horizon']}")

    features = build_book_features(df, cfg)
    feature_cols = (FEATURE_COLUMNS_EXTENDED if cfg["extended"]
                    else FEATURE_COLUMNS_BASE)
    features = features[feature_cols]

    # raw target + samples on RAW features first (split by time), because
    # normalization must be fitted on the train slice only
    if cfg["target_mode"] == "multi_horizon":
        horizons = [int(h) for h in cfg.get("multi_horizons", [6, 12])]
        target = build_multi_horizon_target(df, horizons)
    else:
        target = build_target(df, int(cfg["horizon"]))
    X, y, idxs = make_windowed_samples(features, target, int(cfg["window"]))
    if len(idxs) == 0:
        raise ValueError("no complete samples (target horizon ate the tail?)")

    ratios = (cfg["split"]["train"], cfg["split"]["valid"], cfg["split"]["test"])
    tr_end, va_end = split_indices_time_ordered(len(y), ratios)

    # ---- fit normalization on the TRAIN slice of the FEATURE rows only ----
    # train samples cover feature rows [idxs[0]-window+1 .. idxs[tr_end-1]]
    first_train_row = int(idxs[0]) - int(cfg["window"]) + 1
    train_features = features.iloc[first_train_row:int(idxs[tr_end - 1]) + 1]
    params = fit_normalization(train_features, cfg["normalization"])
    features_norm = apply_normalization(features, params)

    Xn, yn, _ = make_windowed_samples(features_norm, target, int(cfg["window"]))

    # target normalization (train-only statistics) - keeps MSE well-scaled
    y_train_raw = yn[:tr_end]
    y_center = np.nanmean(y_train_raw, axis=0) if len(y_train_raw) else 0.0
    y_scale_arr = np.nanstd(y_train_raw, axis=0) if len(y_train_raw) else np.ones(
        yn.shape[1] if yn.ndim == 2 else 1)
    y_scale_arr = np.where(y_scale_arr > 1e-12, y_scale_arr, 1.0)
    yn_norm = (yn - y_center) / y_scale_arr

    samples = BookSamples(
        X_train=Xn[:tr_end], y_train=yn_norm[:tr_end],
        X_valid=Xn[tr_end:va_end], y_valid=yn_norm[tr_end:va_end],
        X_test=Xn[va_end:], y_test=yn_norm[va_end:],
        norm_params=params, feature_columns=feature_cols,
        target_scale=y_scale_arr,
    )
    if norm_params_path:
        save_normalization_params(params, norm_params_path)
        logger.info("saved normalization parameters to %s", norm_params_path)
    return samples


def synthetic_ohlcv(n: int = 1200, seed: int = 7,
                    start_price: float = 2300.0) -> pd.DataFrame:
    """Deterministic synthetic XAUUSD-like M5 bars for tests/demos when no
    terminal history is available (geometric random walk with regime shifts
    and wick noise)."""
    rng = np.random.default_rng(seed)
    drift = np.resize(rng.normal(0.0, 0.0004, size=max(1, int(np.ceil(n / 120)))), n)
    ret = rng.normal(0.0, 0.0012, size=n) + drift
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[start_price], close[:-1]])
    span = np.abs(rng.normal(0.0, 0.0015, size=n)) * close
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 1.0, size=n)) * span * 0.6
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 1.0, size=n)) * span * 0.6
    volume = rng.integers(80, 400, size=n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume}, index=idx)
