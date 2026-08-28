"""Tests for scripts/audit_weekend_tags.py.

FX trades at Sunday 21:00-24:00 UTC must NOT be tagged 'weekend'.
The audit reads trade_quality CSVs and exits 1 on any violation.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.audit_weekend_tags import audit_weekend_tags, main


def _make_csv(tmp_path, rows, asset="EURUSD"):
    """Write a minimal trade_quality CSV and return its path."""
    path = tmp_path / f"trade_quality_{asset.lower()}_dir.csv"
    fieldnames = [
        "fold_id", "variant", "entry_ts", "direction", "session",
        "regime", "p_long", "p_short", "p_max", "pnl", "R", "exit_reason",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return path


def _ts(year, month, day, hour, minute=0):
    """Epoch seconds for a UTC timestamp."""
    import datetime as dt
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_clean_fx_no_violations(tmp_path):
    """EURUSD trades at Sunday 21+ tagged 'asia' (or any non-weekend) -> OK."""
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 3, 1, 22, 0), "session": "asia", "direction": "long",
         "R": "0.5", "exit_reason": "breakeven"},
        {"entry_ts": _ts(2026, 3, 8, 21, 15), "session": "london", "direction": "short",
         "R": "-0.3", "exit_reason": "stop"},
    ], asset="EURUSD")
    assert audit_weekend_tags(str(tmp_path)) == []


def test_weekend_tag_at_sunday_21_is_violation(tmp_path):
    """EURUSD trade at Sunday 21:00 UTC tagged 'weekend' -> violation."""
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 3, 1, 21, 0), "session": "weekend", "direction": "short",
         "R": "-1.1", "exit_reason": "stop"},
    ], asset="EURUSD")
    violations = audit_weekend_tags(str(tmp_path))
    assert len(violations) == 1
    assert violations[0]["asset"] == "EURUSD"
    assert violations[0]["day"] == "Sunday"


def test_weekend_tag_at_sunday_23_is_violation(tmp_path):
    """XAUUSD trade at Sunday 23:00 UTC tagged 'weekend' -> violation."""
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 6, 28, 23, 0), "session": "weekend", "direction": "long",
         "R": "1.1", "exit_reason": "tp3_runner"},
    ], asset="XAUUSD")
    violations = audit_weekend_tags(str(tmp_path))
    assert len(violations) == 1
    assert violations[0]["asset"] == "XAUUSD"


def test_weekend_tag_on_saturday_not_checked(tmp_path):
    """Weekend tag on Saturday is outside the Sunday 21-24 window -> no violation."""
    import datetime as dt
    # Saturday 22:00 UTC
    ts = int(dt.datetime(2026, 3, 7, 22, 0, tzinfo=dt.timezone.utc).timestamp())
    _make_csv(tmp_path, [
        {"entry_ts": ts, "session": "weekend", "direction": "long",
         "R": "0.1", "exit_reason": "breakeven"},
    ], asset="GBPUSD")
    # Saturday is weekday 5, not 6 (Sunday), so no violation
    assert audit_weekend_tags(str(tmp_path)) == []


def test_weekend_tag_on_monday_not_checked(tmp_path):
    """Weekend tag on Monday is outside the Sunday 21-24 window -> no violation."""
    import datetime as dt
    ts = int(dt.datetime(2026, 3, 2, 22, 0, tzinfo=dt.timezone.utc).timestamp())
    _make_csv(tmp_path, [
        {"entry_ts": ts, "session": "weekend", "direction": "short",
         "R": "-0.5", "exit_reason": "stop"},
    ], asset="EURUSD")
    assert audit_weekend_tags(str(tmp_path)) == []


def test_weekend_tag_before_21_not_checked(tmp_path):
    """Weekend tag at Sunday 20:00 UTC is before 21 -> no violation."""
    import datetime as dt
    ts = int(dt.datetime(2026, 3, 1, 20, 0, tzinfo=dt.timezone.utc).timestamp())
    _make_csv(tmp_path, [
        {"entry_ts": ts, "session": "weekend", "direction": "short",
         "R": "-0.1", "exit_reason": "breakeven"},
    ], asset="XAGUSD")
    assert audit_weekend_tags(str(tmp_path)) == []


def test_btcusd_excluded(tmp_path):
    """BTCUSD is 24/7 -- weekend tag is not checked."""
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 3, 1, 22, 0), "session": "weekend", "direction": "short",
         "R": "-0.2", "exit_reason": "timeout"},
    ], asset="BTCUSD")
    assert audit_weekend_tags(str(tmp_path)) == []


def test_multiple_violations_counted(tmp_path):
    """Multiple FX weekend violations across assets are all counted."""
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 3, 1, 21, 0), "session": "weekend", "direction": "short",
         "R": "-1.0", "exit_reason": "stop"},
        {"entry_ts": _ts(2026, 3, 8, 22, 0), "session": "weekend", "direction": "short",
         "R": "-0.5", "exit_reason": "stop"},
    ], asset="EURUSD")
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 3, 1, 23, 0), "session": "weekend", "direction": "long",
         "R": "0.8", "exit_reason": "tp3_runner"},
    ], asset="GBPUSD")
    violations = audit_weekend_tags(str(tmp_path))
    assert len(violations) == 3


def test_main_exit_code(tmp_path, monkeypatch):
    """main() returns exit 1 on violations, 0 on clean."""
    # Violation
    _make_csv(tmp_path, [
        {"entry_ts": _ts(2026, 3, 1, 22, 0), "session": "weekend", "direction": "short",
         "R": "-1.0", "exit_reason": "stop"},
    ], asset="EURUSD")
    monkeypatch.setattr("scripts.audit_weekend_tags.audit_weekend_tags",
                        lambda log_dir: [{"asset": "EURUSD", "csv": "x", "entry_ts": 0,
                                          "entry_utc": "x", "day": "Sunday",
                                          "direction": "short", "R": "-1.0",
                                          "exit_reason": "stop"}])
    assert main(["--log-dir", str(tmp_path)]) == 1


def test_main_exit_code_clean(monkeypatch):
    """main() returns exit 0 when no violations."""
    monkeypatch.setattr("scripts.audit_weekend_tags.audit_weekend_tags",
                        lambda log_dir: [])
    assert main(["--log-dir", "/nonexistent"]) == 0
