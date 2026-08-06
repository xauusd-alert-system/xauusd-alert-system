"""
Label generator for supervised learning.

Supports:
- fixed barriers: absolute target/stop distances
- atr_scaled barriers: target/stop distances derived from the row's ATR

CRITICAL NO-LOOK-AHEAD WARNING:
This module is intentionally forward-looking for OFFLINE labeling only.
"""
import numpy as np
import pandas as pd


def generate_labels(df: pd.DataFrame, target_x: float, stop_y: float, horizon_n: int,
                     price_col: str = "close") -> pd.Series:
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    entry_prices = df[price_col].values

    labels = np.full(n, np.nan)

    for i in range(n):
        if i + horizon_n >= n:
            continue

        entry = entry_prices[i]
        upper_barrier = entry + target_x
        lower_barrier = entry - stop_y

        outcome = 0
        for j in range(i + 1, i + horizon_n + 1):
            hit_upper = highs[j] >= upper_barrier
            hit_lower = lows[j] <= lower_barrier
            if hit_upper and hit_lower:
                outcome = -1
                break
            elif hit_upper:
                outcome = 1
                break
            elif hit_lower:
                outcome = -1
                break

        labels[i] = outcome

    return pd.Series(labels, index=df.index, name="label")


def generate_labels_atr_scaled(
    df: pd.DataFrame,
    target_atr_multiplier: float,
    stop_atr_multiplier: float,
    horizon_n: int,
    price_col: str = "close",
    atr_col: str = "atr",
) -> pd.Series:
    """
    Triple-barrier labels with per-row barrier widths scaled by ATR.
    Rows with missing/nonpositive ATR are left as NaN.
    """
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    entry_prices = df[price_col].values
    atr_values = df[atr_col].values

    labels = np.full(n, np.nan)

    for i in range(n):
        if i + horizon_n >= n:
            continue

        atr_i = atr_values[i]
        if pd.isna(atr_i) or atr_i <= 0:
            continue

        entry = entry_prices[i]
        upper_barrier = entry + atr_i * target_atr_multiplier
        lower_barrier = entry - atr_i * stop_atr_multiplier

        outcome = 0
        for j in range(i + 1, i + horizon_n + 1):
            hit_upper = highs[j] >= upper_barrier
            hit_lower = lows[j] <= lower_barrier
            if hit_upper and hit_lower:
                outcome = -1
                break
            elif hit_upper:
                outcome = 1
                break
            elif hit_lower:
                outcome = -1
                break

        labels[i] = outcome

    return pd.Series(labels, index=df.index, name="label")


def generate_labels_from_config(df: pd.DataFrame, cfg: dict) -> pd.Series:
    lab_cfg = cfg["labeling"]
    method = lab_cfg.get("method", "fixed")

    if method == "fixed":
        return generate_labels(
            df,
            target_x=lab_cfg["target_pips_x"],
            stop_y=lab_cfg["stop_pips_y"],
            horizon_n=lab_cfg["horizon_candles_n"],
        )

    if method == "atr_scaled":
        return generate_labels_atr_scaled(
            df,
            target_atr_multiplier=lab_cfg["target_atr_multiplier"],
            stop_atr_multiplier=lab_cfg["stop_atr_multiplier"],
            horizon_n=lab_cfg["horizon_candles_n"],
            atr_col=lab_cfg.get("atr_column", "atr"),
        )

    raise ValueError(f"Unknown labeling.method: {method}")


def label_distribution_summary(labels: pd.Series) -> dict:
    valid = labels.dropna()
    total = len(valid)
    if total == 0:
        return {"total_valid": 0}
    return {
        "total_valid": total,
        "pct_upper_hit": float((valid == 1).sum() / total * 100),
        "pct_lower_hit": float((valid == -1).sum() / total * 100),
        "pct_no_hit": float((valid == 0).sum() / total * 100),
        "nan_count": int(labels.isna().sum()),
    }
