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
    """Config-window session tag with explicit weekend exclusion.

    Saturdays/Sundays (UTC) are labeled ``weekend`` — never asia/london/newyork
    — and every other bar gets the config-window label (``off_session`` outside
    all windows). This is the canonical storage label used by backfills, so the
    ``session`` column in ohlcv_* matches both the config windows and the live
    tagger's ``off_session`` for hours outside 0-22 UTC.
    """
    if isinstance(utc_timestamp, (int, float)):
        ts = pd.to_datetime(utc_timestamp, unit="s", utc=True)
    else:
        ts = pd.Timestamp(utc_timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
    if ts.weekday() >= 5:
        return "weekend"
    return tag_session(ts, sessions_config)


def tag_dataframe(df: pd.DataFrame, sessions_config: dict, ts_col: str = "timestamp_utc") -> pd.DataFrame:
    """Vectorized-ish session tagging applied row-wise (fine for daily ingestion batch sizes)."""
    df = df.copy()
    df["session"] = df[ts_col].apply(lambda t: tag_session(t, sessions_config))
    return df
