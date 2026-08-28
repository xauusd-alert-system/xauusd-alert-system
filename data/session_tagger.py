"""
Session tagging based on UTC hour-of-day.
Session windows are read from config.yaml, not hardcoded here.
A candle can belong to multiple overlapping sessions (e.g. London+NY overlap);
in that case we tag with a combined label so backtest/metrics.py can still
report clean per-session and per-overlap breakdowns.
"""

import pandas as pd


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    """Handle windows that do not wrap midnight (all configured windows here don't)."""
    return start <= hour < end


def tag_session(utc_timestamp, sessions_config: dict) -> str:
    """
    Given a UTC epoch-second timestamp (or pandas Timestamp) and the sessions
    sub-dict from config.yaml, return a session label string.
    """
    if isinstance(utc_timestamp, (int, float)):
        ts = pd.to_datetime(utc_timestamp, unit="s", utc=True)
    else:
        ts = pd.Timestamp(utc_timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")

    hour = ts.hour
    active = []
    for name, window in sessions_config.items():
        if _hour_in_window(hour, window["start"], window["end"]):
            active.append(name)

    if not active:
        return "off_session"
    return "_".join(sorted(active))


def tag_session_with_weekend(utc_timestamp, sessions_config: dict) -> str:
    """Session tag that handles the FX weekend boundary.

    Saturday is always 'weekend'.
    Sunday 00:00-20:59 UTC is 'weekend' (market closed).
    Sunday 21:00+ UTC is the FX market reopen — classify by hour into
    the appropriate session (usually 'newyork' at 21-22 UTC).  This
    prevents phantom 'weekend' trades during the first real session of
    the week.
    Weekdays use the standard session windows from config.
    """
    if isinstance(utc_timestamp, (int, float)):
        ts = pd.to_datetime(utc_timestamp, unit="s", utc=True)
    else:
        ts = pd.Timestamp(utc_timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")

    weekday = ts.weekday()  # Mon=0 .. Sun=6
    # Saturday: always weekend
    if weekday == 5:
        return "weekend"
    # Sunday before 21:00: market still closed
    if weekday == 6 and ts.hour < 21:
        return "weekend"
    # Sunday 21:00+ and all weekdays: classify by session windows
    return tag_session(ts, sessions_config)


def tag_dataframe(df: pd.DataFrame, sessions_config: dict, ts_col: str = "timestamp_utc") -> pd.DataFrame:
    """Vectorized-ish session tagging applied row-wise (fine for daily ingestion batch sizes)."""
    df = df.copy()
    df["session"] = df[ts_col].apply(lambda t: tag_session(t, sessions_config))
    return df
