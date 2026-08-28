"""
Unsupervised Market Regime Classifier.
Uses a Gaussian Mixture Model (GMM) to discover latent market regimes
(e.g., low-volatility trend, high-volatility range, compression, shock)
from causal technical features.

NOTE: despite the historical file name, this is a GMM, not an HMM — no
temporal transition modelling is performed. Kept under this name to avoid
churn; the class is currently NOT wired into any production pipeline.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler


class UnsupervisedRegimeClassifier:
    """
    Learns K distinct market regimes from causal technical features:
    returns, log volatility, normalized volume, and trend slope.
    """

    def __init__(self, n_regimes: int = 4, random_state: int = 42):
        self.n_regimes = int(n_regimes)
        self.random_state = random_state
        self.scaler = StandardScaler()
        self.gmm = GaussianMixture(
            n_components=self.n_regimes,
            covariance_type="full",
            random_state=self.random_state,
            max_iter=100,
        )
        self.is_fitted = False
        self.cluster_labels: Dict[int, str] = {}

    def _extract_features(self, df: pd.DataFrame) -> pd.DataFrame:
        returns = df["close"].pct_change().fillna(0.0)
        vol = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        vol = vol.fillna(0.0)
        vol_ma = vol.rolling(window=20, min_periods=1).mean()
        rel_vol = (vol / vol_ma.replace(0, np.nan)).fillna(1.0)

        volume = df["volume"] if "volume" in df.columns else pd.Series(1.0, index=df.index)
        vol_sma = volume.rolling(window=20, min_periods=1).mean()
        norm_volume = (volume / vol_sma.replace(0, np.nan)).fillna(1.0)

        slope = (df["close"] - df["close"].shift(10)) / df["close"].shift(10).replace(0, np.nan)
        slope = slope.fillna(0.0)

        feats = pd.DataFrame({
            "return": returns,
            "rel_volatility": rel_vol,
            "norm_volume": norm_volume,
            "slope": slope,
        }, index=df.index)
        return feats

    def fit(self, df: pd.DataFrame) -> "UnsupervisedRegimeClassifier":
        feats = self._extract_features(df)
        X_scaled = self.scaler.fit_transform(feats)
        self.gmm.fit(X_scaled)
        self.is_fitted = True

        # Assign descriptive labels based on cluster means
        means = self.gmm.means_
        # Feature order: 0: return, 1: rel_volatility, 2: norm_volume, 3: slope
        for k in range(self.n_regimes):
            mean_vol = means[k, 1]
            mean_slope = means[k, 3]
            if mean_vol > 0.5:
                label = "high_vol_trending" if abs(mean_slope) > 0.2 else "high_vol_shock"
            elif mean_vol < -0.5:
                label = "low_vol_compression"
            else:
                label = "standard_range" if abs(mean_slope) < 0.3 else "directional_trend"
            self.cluster_labels[k] = label

        return self

    def _require_fitted(self, df: pd.DataFrame) -> None:
        """Audit A 2026-08-23: the old auto-fit-on-predict trained the scaler
        and GMM on the very frame being predicted (transductive leakage in any
        walk-forward use). Fail loudly instead."""
        if not self.is_fitted:
            raise RuntimeError(
                "UnsupervisedRegimeClassifier.predict_* called before fit(). "
                "Auto-fitting on the prediction frame would leak test data "
                "into the model. Call fit() on a training frame first."
            )

    def predict_regime(self, df: pd.DataFrame) -> pd.Series:
        """Predicts causal regime labels for the dataframe."""
        self._require_fitted(df)

        feats = self._extract_features(df)
        X_scaled = self.scaler.transform(feats)
        clusters = self.gmm.predict(X_scaled)
        labels = [self.cluster_labels.get(c, f"regime_{c}") for c in clusters]
        return pd.Series(labels, index=df.index)

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        """Returns regime posterior probability matrix."""
        self._require_fitted(df)

        feats = self._extract_features(df)
        X_scaled = self.scaler.transform(feats)
        return self.gmm.predict_proba(X_scaled)
