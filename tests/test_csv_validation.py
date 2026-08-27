"""Tests for CSV replay validation (P1-7)."""
import tempfile
from pathlib import Path
import pytest

from usstocks.data.replay_provider import load_bars


def test_load_bars_valid_csv():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as tf:
        tf.write(
            "symbol,ts,open,high,low,close,volume\n"
            "AAPL,2026-08-27T09:30:00-04:00,150.0,151.0,149.5,150.5,1000\n"
            "AAPL,2026-08-27T09:31:00-04:00,150.5,152.0,150.0,151.8,1200\n"
        )
        tf_path = tf.name

    bars = load_bars(tf_path)
    assert len(bars) == 2
    assert bars[0].open == 150.0
    assert bars[1].close == 151.8
    Path(tf_path).unlink()


def test_load_bars_missing_header():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as tf:
        tf.write("symbol,ts,open,high\n")  # missing low, close
        tf_path = tf.name

    with pytest.raises(ValueError, match="missing required columns"):
        load_bars(tf_path)
    Path(tf_path).unlink()


def test_load_bars_invalid_price():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as tf:
        tf.write(
            "symbol,ts,open,high,low,close,volume\n"
            "AAPL,2026-08-27T09:30:00-04:00,150.0,INVALID,149.5,150.5,1000\n"
        )
        tf_path = tf.name

    with pytest.raises(ValueError, match="parse error"):
        load_bars(tf_path)
    Path(tf_path).unlink()


def test_load_bars_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        load_bars("nonexistent_path_file_123.csv")
