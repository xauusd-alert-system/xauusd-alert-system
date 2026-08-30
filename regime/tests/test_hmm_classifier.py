"""
Tests for Unsupervised Regime Classifier (GMM / Latent states).
"""

import numpy as np
import pandas as pd
import pytest

from regime.hmm_classifier import UnsupervisedRegimeClassifier


@pytest.fixture
def dummy_ohlcv():
    np.random.seed(42)
    n = 300
    close = 2000.0 + np.cumsum(np.random.randn(n) * 2.0)
    high = close + np.abs(np.random.randn(n) * 1.5)
    low = close - np.abs(np.random.randn(n) * 1.5)
    open_p = (high + low) / 2.0
    vol = np.random.randint(50, 1000, size=n).astype(float)
    return pd.DataFrame(
        {
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        }
    )


def test_unsupervised_regime_fit_predict(dummy_ohlcv):
    clf = UnsupervisedRegimeClassifier(n_regimes=4, random_state=42)
    clf.fit(dummy_ohlcv)
    assert clf.is_fitted is True

    labels = clf.predict_regime(dummy_ohlcv)
    assert len(labels) == len(dummy_ohlcv)
    assert isinstance(labels.iloc[0], str)

    probs = clf.predict_proba(dummy_ohlcv)
    assert probs.shape == (len(dummy_ohlcv), 4)
    assert np.allclose(probs.sum(axis=1), 1.0)
