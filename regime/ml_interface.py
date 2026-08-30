"""
Abstract interface for a future ML-based regime classifier.

Design decision: the rule-based classifier ships first (regime/classifier.py).
This interface defines the CONTRACT any future ML regime model must satisfy so
it can be swapped in without touching regime/classifier.py callers (labeling/,
backtest/, realtime/pipeline.py).

Contract: .classify(row: pd.Series) -> RegimeLabel
          .classify_series(df: pd.DataFrame) -> pd.Series[RegimeLabel]

Any concrete ML implementation (e.g. a trained sklearn/XGBoost multi-class model)
should subclass RegimeClassifierBase and implement these two methods.
"""

from abc import ABC, abstractmethod

import pandas as pd

from regime.classifier import RegimeLabel


class RegimeClassifierBase(ABC):
    """Abstract base - both rule-based and future ML regime classifiers implement this."""

    @abstractmethod
    def classify(self, row: pd.Series) -> RegimeLabel:
        """Classify a single row into a RegimeLabel."""
        raise NotImplementedError

    @abstractmethod
    def classify_series(self, df: pd.DataFrame) -> pd.Series:
        """Classify an entire DataFrame, row by row, returning a Series of RegimeLabel."""
        raise NotImplementedError


class RuleBasedRegimeClassifier(RegimeClassifierBase):
    """Adapter wrapping the existing rule-based functions to satisfy the ABC contract."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def classify(self, row: pd.Series) -> RegimeLabel:
        from regime.classifier import classify_regime_row

        return classify_regime_row(row, self.cfg)

    def classify_series(self, df: pd.DataFrame) -> pd.Series:
        from regime.classifier import classify_regime_series

        return classify_regime_series(df, self.cfg)


class MLRegimeClassifierStub(RegimeClassifierBase):
    """
    Placeholder for a future trained ML regime classifier.
    NOT implemented yet per project plan (Step 4 = rule-based only, ML comes in Step 8+).
    Raises NotImplementedError intentionally so it fails loudly if wired in prematurely.
    """

    def __init__(self, model_path: str = None):
        self.model_path = model_path
        self.model = None  # would be loaded from model_path in a real implementation

    def classify(self, row: pd.Series) -> RegimeLabel:
        raise NotImplementedError("ML regime classifier not yet trained - use RuleBasedRegimeClassifier.")

    def classify_series(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError("ML regime classifier not yet trained - use RuleBasedRegimeClassifier.")
