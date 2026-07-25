"""
Label generator for supervised learning: "does price reach +X before -Y within N candles".

This is a triple-barrier-style label:
  - Upper barrier: entry_price + X (in points)
  - Lower barrier: entry_price - Y (in points)
  - Time barrier: N candles ahead

Label values:
   1  -> upper barrier hit first (long-favorable outcome)
  -1  -> lower barrier hit first (short-favorable outcome, i.e. price fell -Y before rising +X)
   0  -> neither barrier hit within N candles (time barrier expired) -> no clear outcome
  NaN -> insufficient future data to evaluate (near the end of the dataset)

CRITICAL NO-LOOK-AHEAD WARNING:
This label generator is THE ONE PLACE in the entire codebase that is INTENTIONALLY
look-ahead by design - because labels for supervised learning MUST be derived from
future price action (that is the entire point of a label). This must NEVER be used
as a live feature; it exists ONLY for offline training/backtesting where the row's
label is understood to be unavailable at prediction time. backtest/engine.py and
realtime/pipeline.py MUST NEVER read label columns produced here as if they were
known at the time of the row's timestamp - they represent the OUTCOME AFTER the row.
X and Y and N are read entirely from config.yaml (labeling: target_pips_x, stop_pips_y,
horizon_candles_n) - never hardcoded.
"""
import numpy as np
import pandas as pd


def generate_labels(df: pd.DataFrame, target_x: float, stop_y: float, horizon_n: int,
                     price_col: str = "close") -> pd.Series:
    """
    df must be sorted ascending by timestamp_utc, with high/low/close columns.
    Uses high/low of FUTURE candles (i+1 .. i+horizon_n) to check barrier hits -
    this future lookup is the intentional, documented exception described above.
    Returns a pd.Series aligned to df.index with values in {1, -1, 0, NaN}.
    """
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    entry_prices = df[price_col].values

    labels = np.full(n, np.nan)

    for i in range(n):
        if i + horizon_n >= n:
            continue  # not enough future candles to evaluate - leave as NaN, never guess

        entry = entry_prices[i]
        upper_barrier = entry + target_x
        lower_barrier = entry - stop_y

        outcome = 0  # default: time barrier expires with no hit
        for j in range(i + 1, i + horizon_n + 1):
            hit_upper = highs[j] >= upper_barrier
            hit_lower = lows[j] <= lower_barrier
            if hit_upper and hit_lower:
                # Ambiguous same-candle double-touch: conservatively assume the WORSE
                # outcome for the trader was hit first (stop-loss), since intra-candle
                # order of high/low touches is unknown from OHLC data alone.
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
    """Convenience wrapper reading X/Y/N from config.yaml's `labeling:` block."""
    lab_cfg = cfg["labeling"]
    return generate_labels(
        df,
        target_x=lab_cfg["target_pips_x"],
        stop_y=lab_cfg["stop_pips_y"],
        horizon_n=lab_cfg["horizon_candles_n"],
    )


def label_distribution_summary(labels: pd.Series) -> dict:
    """
    Statistical validation helper: returns counts/percentages of each label class.
    Used in labeling/tests and in the visual validation script to sanity-check that
    labels are not massively skewed to one class (which would indicate X/Y misconfiguration
    or a data issue) and that the NaN tail at the end of the series matches horizon_n.
    """
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
