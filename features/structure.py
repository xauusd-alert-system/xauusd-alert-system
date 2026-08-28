"""
Structure highs/lows detection (swing points).

CRITICAL NO-LOOK-AHEAD NOTE:
A "true" swing high at index i is normally defined by comparing candle i to
K candles on BOTH sides (i-K..i+K). That definition is look-ahead by nature -
you cannot confirm a swing high until K candles AFTER it close.
To keep this causal for real-time use, we implement a CONFIRMED swing detector:
the swing at index (i - lookback) is only marked/confirmed once we have processed
candle i, i.e. the swing label appears with a K-candle delay. This delay is
intentional and documented - callers must be aware a swing flagged at row j means
"a swing was confirmed AS OF this row, referring to a candle `lookback` rows ago".
We never let row i's OWN swing_high/swing_low flag depend on rows AFTER i.
"""

import numpy as np
import pandas as pd


def detect_structure(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """
    Adds columns:
      swing_high_confirmed: True at row i if the candle at (i - lookback) is a
                             confirmed local high relative to the lookback-window on ITS left
                             and the lookback rows up to and including row i on its right.
      swing_low_confirmed: symmetric for lows.
      last_structure_high / last_structure_low: forward-filled most recent confirmed level,
                             known at row i using only data up to row i (causal).
    """
    out = df.copy()
    n = len(out)
    swing_high = np.zeros(n, dtype=bool)
    swing_low = np.zeros(n, dtype=bool)

    highs = out["high"].values
    lows = out["low"].values

    # Confirm swing at candidate index c only once we have `lookback` candles
    # AFTER c (i.e. current index i = c + lookback)
    for i in range(lookback * 2, n):
        c = i - lookback  # candidate swing index, now fully bracketed by known past+future-up-to-i data
        window_left = highs[c - lookback : c]
        window_right = highs[c + 1 : c + lookback + 1]
        if len(window_left) == lookback and len(window_right) == lookback:
            if highs[c] > window_left.max() and highs[c] > window_right.max():
                swing_high[i] = True  # flag appears at i, referring to candidate c

        window_left_l = lows[c - lookback : c]
        window_right_l = lows[c + 1 : c + lookback + 1]
        if len(window_left_l) == lookback and len(window_right_l) == lookback:
            if lows[c] < window_left_l.min() and lows[c] < window_right_l.min():
                swing_low[i] = True

    out["swing_high_confirmed"] = swing_high
    out["swing_low_confirmed"] = swing_low

    # Track the level value at the confirmed candidate index, forward-filled causally
    confirmed_high_level = pd.Series(
        np.where(swing_high, highs[np.maximum(np.arange(n) - lookback, 0)], np.nan),
        index=out.index,
    )
    confirmed_low_level = pd.Series(
        np.where(swing_low, lows[np.maximum(np.arange(n) - lookback, 0)], np.nan),
        index=out.index,
    )

    out["last_structure_high"] = confirmed_high_level.ffill()
    out["last_structure_low"] = confirmed_low_level.ffill()

    return out
