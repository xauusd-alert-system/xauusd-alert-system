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

import pandas as pd

from model.trainer import load_model


class ModelPredictor:
    """Wraps a loaded, calibrated model + its feature column list for safe inference."""

    def __init__(self, model_path: str):
        bundle = load_model(model_path)
        self.model = bundle["model"]
        self.feature_cols = bundle["feature_cols"]
        # Old bundles remain loadable; newly trained production bundles expose
        # their target/config/weight contract for deployment checks and dashboards.
        self.metadata = bundle.get("metadata", {})

    @property
    def classes_(self):
        """Expose the fitted class labels which determine the encoding endianness."""
        if hasattr(self.model, "classes_"):
            return self.model.classes_
        return None

    def predict_proba(self, row_or_df) -> pd.DataFrame:
        """
        Accepts a single pd.Series (one row) or a pd.DataFrame (batch).
        Returns a DataFrame with columns:
          p_short, p_long                        (binary model; label 1 = long-favorable)
          p_short, p_no_trade, p_long            (3-class model; classes {0,1,2})
        Rows always sum to 1.0. Class ordering is delegated to each fitted model's
        `classes_` because classifiers may sort labels before fitting - NEVER assume
        positional order. Any missing feature column raises explicitly rather than
        silently filling zeros, since silent zero-filling could produce a confidently
        wrong prediction. EXCEPTION (Phase 3): `regime_<label>` one-hot columns that
        are missing are synthesized from a `regime` column (when present) instead of
        raising - this keeps inference working for backtest/realtime DataFrames that
        carry the raw regime label but not the expanded one-hot columns.
        """
        if isinstance(row_or_df, pd.Series):
            df = row_or_df.to_frame().T
        else:
            df = row_or_df

        needed = list(self.feature_cols)
        missing = set(needed) - set(df.columns)
        if missing:
            regime_cols = [c for c in missing if c.startswith("regime_")]
            if regime_cols and "regime" in df.columns:
                # Phase 3: re-expand the causal regime column into the missing
                # one-hot features (identical encoding to build_training_matrix).
                # regime_onehot_df emits the full RegimeLabel set; select only the
                # columns this model actually needs that are still missing.
                from regime.classifier import regime_onehot_df

                onehot = regime_onehot_df(df).reindex(columns=needed)
                add_cols = [c for c in needed if c in onehot.columns and c not in df.columns]
                df = pd.concat([df, onehot[add_cols]], axis=1)
                missing = set(needed) - set(df.columns)
        if missing:
            raise ValueError(f"Missing required feature columns for inference: {missing}")

        X = df[self.feature_cols]
        if X.isnull().any().any():
            raise ValueError("NaN found in feature inputs at inference time - cannot predict on incomplete data.")

        X = X.astype(float)
        proba = self.model.predict_proba(X)

        classes = self.classes_
        if classes is not None:
            # Map each class label to its column index by VALUE (robust to sorting).
            order = {int(c): i for i, c in enumerate(classes)}
            if 2 in order:
                # 3-class encoding {0: short, 1: no_trade, 2: long} (Phase 2).
                return pd.DataFrame(
                    {
                        "p_short": proba[:, order[0]],
                        "p_no_trade": proba[:, order[1]],
                        "p_long": proba[:, order[2]],
                    },
                    index=df.index,
                )
            # Binary encoding {0: short, 1: long} (default Phase-0+1 path).
            return pd.DataFrame({"p_short": proba[:, order[0]], "p_long": proba[:, order[1]]}, index=df.index)

        # Fallback, no classes_ attribute: conventional sklearn binary ordering
        # [P(class=0), P(class=1)] = [P(short), P(long)].
        return pd.DataFrame({"p_short": proba[:, 0], "p_long": proba[:, 1]}, index=df.index)

    def predict_single(self, row: pd.Series) -> dict:
        """Convenience wrapper returning a plain dict for the single-row realtime use case."""
        result = self.predict_proba(row)
        out = {"p_long": float(result["p_long"].iloc[0]), "p_short": float(result["p_short"].iloc[0])}
        if "p_no_trade" in result.columns:
            out["p_no_trade"] = float(result["p_no_trade"].iloc[0])
        return out
