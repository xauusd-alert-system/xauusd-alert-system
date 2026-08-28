"""
Unit tests for data/signal_log.py.
Run with: pytest data/tests/test_signal_log.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.signal_log import init_schema, log_signal, read_signal_history


def _sample_signal(ts=1700000000, bias="long"):
    return {
        "bias": bias,
        "confidence": 0.85,
        "entry_zone": [2400.0, 2400.5] if bias != "no_trade" else None,
        "invalidation": 2398.0 if bias != "no_trade" else None,
        "targets": [2403.0] if bias != "no_trade" else None,
        "reasoning_summary": "test reasoning",
        "regime": "trend_up",
        "timestamp_utc": ts,
        "session": "london",
        "generated_at": "2026-07-24T08:00:00+00:00",
    }


def test_init_schema_creates_table(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)
    assert os.path.exists(db_path)


def test_log_and_read_signal_roundtrip(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)
    signal = _sample_signal()
    log_signal(db_path, signal, alert_sent=True)

    df = read_signal_history(db_path)
    assert len(df) == 1
    assert df.iloc[0]["bias"] == "long"
    assert df.iloc[0]["alert_sent"] == 1
    assert json.loads(df.iloc[0]["entry_zone"]) == [2400.0, 2400.5]


def test_log_signal_idempotent_on_same_timestamp(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)
    signal = _sample_signal(ts=1700000000)
    log_signal(db_path, signal, alert_sent=False)
    signal_updated = _sample_signal(ts=1700000000, bias="short")
    log_signal(db_path, signal_updated, alert_sent=True)

    df = read_signal_history(db_path)
    assert len(df) == 1, "Same timestamp must upsert, not duplicate"
    assert df.iloc[0]["bias"] == "short"
    assert df.iloc[0]["alert_sent"] == 1


def test_log_no_trade_signal_has_null_trade_fields(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)
    signal = _sample_signal(ts=1700000900, bias="no_trade")
    log_signal(db_path, signal, alert_sent=False)

    df = read_signal_history(db_path)
    assert df.iloc[0]["entry_zone"] is None
    assert df.iloc[0]["targets"] is None


def test_read_signal_history_filters_by_time_range(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)
    for i in range(5):
        log_signal(db_path, _sample_signal(ts=1700000000 + i * 900), alert_sent=False)

    df = read_signal_history(db_path, start_ts=1700000900, end_ts=1700002700)
    assert len(df) == 3
    assert df["timestamp_utc"].min() >= 1700000900
    assert df["timestamp_utc"].max() <= 1700002700


def test_read_signal_history_sorted_ascending(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)
    for ts in [1700002700, 1700000000, 1700001800]:
        log_signal(db_path, _sample_signal(ts=ts), alert_sent=False)

    df = read_signal_history(db_path)
    assert list(df["timestamp_utc"]) == sorted(df["timestamp_utc"])
