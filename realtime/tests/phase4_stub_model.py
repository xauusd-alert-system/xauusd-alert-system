"""Module-level stub model for the Phase-4 serve test.

Must live at module level so joblib can pickle/unpickle it by qualname.
Deterministic long-biased probabilities; used only to verify that
ModelPredictor.predict_single does NOT raise on a flag-on pipeline frame
(the column contract, not the statistics).
"""

from __future__ import annotations

import numpy as np


class Phase4StubModel:
    def predict_proba(self, X):
        return np.array([[0.4, 0.6]] * len(X))

    classes_ = np.array([0, 1])
