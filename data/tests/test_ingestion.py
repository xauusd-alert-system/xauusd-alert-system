"""
Unit tests for data/ ingestion, storage, and session tagging.
Run with: pytest data/tests/test_ingestion.py -v
"""
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles, TIMEFRAME_TO_SECONDS
from data.storage import init_schema, upsert_candles, read_candles
from data.session_tagger import tag_session


CFG = load_config()
SESSIONS = CFG["sessions"]


def test_mock_candles_shape():
    df = fetch_mock_candles("M5", n_candles=100, sessions_config=SESSIONS)
    assert len(df) == 100
    assert set(["timestamp_utc", "open", "high", "low", "close", "volume", "session"]) <= set(df.columns)


def test_mock_candles_timestamp_monotonic():
    df = fetch_mock_candles("M15", n_candles=50, sessions_config=SESSIONS)
    diffs = df["timestamp_utc"].diff().dropna()
    assert (diffs == TIMEFRAME_TO_SECONDS["M15"]).all(), "Timestamps must be evenly spaced with no gaps"


def test_mock_candles_ohlc_consistency():
    df = fetch_mock_candles("M1", n_candles=200, sessions_config=SESSIONS)
    assert (df["high"] >= df["open"]).all()
    assert (df["high"] >= df["close"]).all()
    assert (df["low"] <= df["open"]).all()
    assert (df["low"] <= df["close"]).all()


def test_session_tagging_known_hours():
    # 03:00 UTC -> Asia only window (0-8)
    ts_asia = pd.Timestamp("2026-07-24 03:00:00", tz="UTC").timestamp()
    label = tag_session(ts_asia, SESSIONS)
    assert "asia" in label

    # 13:00 UTC -> London (7-16) and NY (12-21) overlap
    ts_overlap = pd.Timestamp("2026-07-24 13:00:00", tz="UTC").timestamp()
    label_overlap = tag_session(ts_overlap, SESSIONS)
    assert "london" in label_overlap and "ny" in label_overlap

    # 23:00 UTC -> off_session (outside all windows)
    ts_off = pd.Timestamp("2026-07-24 23:00:00", tz="UTC").timestamp()
    label_off = tag_session(ts_off, SESSIONS)
    assert label_off == "off_session"


def test_storage_roundtrip(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    init_schema(db_path, ["M5"])
    df = fetch_mock_candles("M5", n_candles=30, sessions_config=SESSIONS)
    upsert_candles(db_path, "M5", df)
    read_back = read_candles(db_path, "M5")
    assert len(read_back) == 30
    assert list(read_back["timestamp_utc"]) == sorted(read_back["timestamp_utc"])


def test_storage_idempotent_upsert(tmp_path):
    """Re-inserting the same candles must not create duplicates (idempotency check)."""
    db_path = str(tmp_path / "test2.sqlite")
    init_schema(db_path, ["M1"])
    df = fetch_mock_candles("M1", n_candles=20, sessions_config=SESSIONS)
    upsert_candles(db_path, "M1", df)
    upsert_candles(db_path, "M1", df)  # insert same rows again
    read_back = read_candles(db_path, "M1")
    assert len(read_back) == 20  # no duplicates due to INSERT OR REPLACE on PK
