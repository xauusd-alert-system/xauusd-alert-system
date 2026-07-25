"""
Model inference: load a trained/calibrated model and produce P(long)/P(short) for a
single row (or batch) of features at prediction time.

CRITICAL NO-LOOK-AHEAD NOTE:
predict() only ever consumes FEATURE_COLUMNS (causal, from features/) - it must NEVER
be given a row that also contains the 'label' column, since label is only meaningful
in hindsight (see model/trainer.py docstring). This module enforces that by explicitly
selecting only the trained feature_cols list saved alongside the model, ignoring any
extra columns (including 'label' if accidentally present) in the input DataFrame.
"""
import numpy as np
import pandas as pd

from model.trainer import load_model, FEATURE_COLUMNS


class ModelPredictor:
    """Wraps a loaded, calibrated model + its feature column list for safe inference."""

    def __init__(self, model_path: str):
        bundle = load_model(model_path)
        self.model = bundle["model"]
        self.feature_cols = bundle["feature_cols"]

    def predict_proba(self, row_or_df) -> pd.DataFrame:
        """
        Accepts a single pd.Series (one row) or a pd.DataFrame (batch).
        Returns a DataFrame with columns: p_long, p_short - guaranteed to sum to 1.0
        per row since this is a binary classifier (label 1 = long-favorable outcome).
        Any missing feature column raises explicitly rather than silently filling zeros,
        since silent zero-filling could produce a confidently wrong prediction.
        """
        if isinstance(row_or_df, pd.Series):
            df = row_or_df.to_frame().T
        else:
            df = row_or_df

        missing = set(self.feature_cols) - set(df.columns)
        if missing:
            raise ValueError(f"Missing required feature columns for inference: {missing}")

        X = df[self.feature_cols]
        if X.isnull().any().any():
            raise ValueError("NaN found in feature inputs at inference time - cannot predict on incomplete data.")

        proba = self.model.predict_proba(X)  # columns: [P(class=0), P(class=1)] = [P(short), P(long)]
        X = X.astype(float)
        return pd.DataFrame({"p_short": proba[:, 0], "p_long": proba[:, 1]}, index=df.index)

    def predict_single(self, row: pd.Series) -> dict:
        """Convenience wrapper returning a plain dict for the single-row realtime use case."""
        result = self.predict_proba(row)
        return {"p_long": float(result["p_long"].iloc[0]), "p_short": float(result["p_short"].iloc[0])}
