"""
Multi-timeframe confluence score.

Combines directional bias signals from higher timeframes (e.g. M15, H1, H4) into
a single confluence score for a base timeframe (e.g. M5) row.

CRITICAL NO-LOOK-AHEAD NOTE:
When merging a higher-timeframe (HTF) feature onto a lower-timeframe (LTF) row,
we must only use the HTF candle that had ALREADY CLOSED at or before the LTF
candle's timestamp. merge_asof with direction="backward" enforces exactly this:
it matches each LTF timestamp to the most recent HTF timestamp <= it, never a
future HTF candle. This is the single most common source of look-ahead bias in
multi-timeframe systems, so it is isolated and heavily commented here.
"""

import pandas as pd


def merge_htf_feature(ltf_df: pd.DataFrame, htf_df: pd.DataFrame, feature_col: str, out_col_name: str) -> pd.DataFrame:
    """
    Backward-merge a single HTF feature column onto the LTF DataFrame using merge_asof.
    Both DataFrames must be sorted ascending by timestamp_utc (enforced here defensively).
    """
    ltf_sorted = ltf_df.sort_values("timestamp_utc").reset_index(drop=True)
    htf_sorted = htf_df[["timestamp_utc", feature_col]].sort_values("timestamp_utc").reset_index(drop=True)
    htf_sorted = htf_sorted.rename(columns={feature_col: out_col_name})
    # AUDIT 2026-08-23 (module 8b): HTF bars are stamped at bar OPEN. A plain
    # backward merge lets an LTF row inside [T, T+dT) receive HTF bar T's FINAL
    # close — information that does not exist until T+dT. Offline training reads
    # those bars from history (leak), while live inference only ever sees closed
    # bars -> systematic train/serve skew. Shifting by one HTF row means an LTF
    # row can only see the previous COMPLETED HTF bar; the first row becomes NaN
    # and stays neutral downstream (pandas sum skips NaN).
    htf_sorted[out_col_name] = htf_sorted[out_col_name].shift(1)

    merged = pd.merge_asof(
        ltf_sorted,
        htf_sorted,
        on="timestamp_utc",
        direction="backward",  # NEVER "forward" or "nearest" - would leak future HTF candles
    )
    return merged


def compute_confluence_score(ltf_df: pd.DataFrame, htf_frames: dict, cfg: dict) -> pd.DataFrame:
    """
    htf_frames: dict of {timeframe_name: htf_dataframe_with_indicators}
    For each HTF, derive a simple directional vote: +1 if close > ema_50, -1 if close < ema_50, else 0.
    Confluence score = sum of votes across configured mtf_reference_timeframes, normalized to [-1, 1].
    """
    out = ltf_df.copy()
    votes = []
    ref_tfs = cfg["features"]["mtf_reference_timeframes"]

    for tf_name in ref_tfs:
        htf_df = htf_frames.get(tf_name)
        if htf_df is None:
            continue
        htf_df = htf_df.copy()
        htf_df["htf_vote"] = 0
        htf_df.loc[htf_df["close"] > htf_df["ema_50"], "htf_vote"] = 1
        htf_df.loc[htf_df["close"] < htf_df["ema_50"], "htf_vote"] = -1

        out = merge_htf_feature(out, htf_df, "htf_vote", f"vote_{tf_name}")
        votes.append(f"vote_{tf_name}")

    if votes:
        out["mtf_confluence_score"] = out[votes].sum(axis=1) / len(votes)
    else:
        out["mtf_confluence_score"] = 0.0

    return out
