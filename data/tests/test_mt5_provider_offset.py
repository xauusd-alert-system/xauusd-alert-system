"""N10 (audit 2026-08-10): MT5 bar timestamps are in BROKER-SERVER time, not UTC.

FxPro's server runs EET/EEST (UTC+2/+3), so raw `copy_rates_*` epochs must be
shifted to true UTC before session tagging / labeling / range slicing. This
tests the shift applied by `_normalize_rates`, that a zero offset preserves
the legacy behaviour, and the startup auto-detection of the broker offset from
a fresh live tick (config server_time_offset_hours: auto).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from data.mt5_provider import _normalize_rates, detect_server_offset_hours, resolve_server_offset

# Pinned "now" for the auto-detect tests: tick.time is expressed relative to
# this, and data.mt5_provider.time.time is patched to return it, so the measured
# delta is exact (no µs gap between tick capture and the now() read).
_BASE_TIME = 1_800_000_000.0


def _fake_rates(n: int = 3, base_time: int = 1_800_000_000) -> np.ndarray:
    """A numpy structured array shaped like mt5.copy_rates output."""
    dtype = [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "i8"),
        ("spread", "i8"),
        ("real_volume", "i8"),
    ]
    rows = []
    for i in range(n):
        rows.append((
            base_time + i * 900,  # M15 grid in BROKER server time
            100.0 + i, 100.5 + i, 99.5 + i, 100.25 + i,
            100 + i, 5, 200 + i,
        ))
    return np.array(rows, dtype=dtype)


def test_offset_three_hours_shifts_to_true_utc():
    rates = _fake_rates(base_time=1_800_000_000)
    df = _normalize_rates(rates, server_offset_hours=3.0)

    assert len(df) == 3
    expected_utc = pd.to_datetime(1_800_000_000 - 3 * 3600, unit="s", utc=True)
    assert df["timestamp"].iloc[0] == expected_utc
    # every bar shifted by the full offset, grid spacing preserved
    assert (df["timestamp"].diff().dropna().dt.total_seconds() == 900).all()


def test_zero_offset_preserves_raw_server_time():
    rates = _fake_rates(base_time=1_800_000_000)
    df = _normalize_rates(rates, server_offset_hours=0.0)

    assert df["timestamp"].iloc[0] == pd.to_datetime(1_800_000_000, unit="s", utc=True)


def test_offset_drops_nothing_and_keeps_price_columns():
    rates = _fake_rates(n=5, base_time=1_800_000_000)
    df = _normalize_rates(rates, server_offset_hours=2.0)

    assert len(df) == 5
    for col in ("open", "high", "low", "close", "volume", "spread", "real_volume"):
        assert col in df.columns
    assert df["close"].tolist() == pytest.approx([100.25, 101.25, 102.25, 103.25, 104.25])


class _FakeMt5:
    """Minimal stand-in for the MetaTrader5 module (initialize/symbol/tick)."""

    def __init__(self, tick_delta_sec: float | None = None, fail_select: bool = False,
                 base_time: float = _BASE_TIME):
        self._delta = tick_delta_sec
        self._fail_select = fail_select
        self._base_time = base_time
        self.initialize_calls = 0

    def initialize(self):
        self.initialize_calls += 1
        return True

    def symbol_select(self, symbol, enable):
        if self._fail_select:
            return False
        return True

    def symbol_info_tick(self, symbol):
        if self._delta is None:
            return None
        return SimpleNamespace(time=self._base_time + self._delta)


def _pin_weekday(monkeypatch):
    """Pin the clock (weekday + now) so the live-tick path is exercised with an
    exact delta and the weekend guard is inert."""
    monkeypatch.setattr("data.mt5_provider.time.time", lambda: _BASE_TIME)
    monkeypatch.setattr("data.mt5_provider._is_weekend_utc", lambda: False)


@pytest.mark.parametrize("delta_sec,expected", [
    (10801, 3.0),   # 3.0003h — EEST summer, the measured FxPro case
    (7201, 2.0),    # 2.0003h — EET winter
    (5400, 2.0),    # 1.5h rounds to 2 (banker's rounding)
    (0, 0.0),       # server == UTC
    (-10801, -3.0), # broker behind UTC (signed, kept)
    (12 * 3600, 12.0),  # large but within real timezones (UTC+12) — trusted
])
def test_detect_offset_fresh_tick_rounds_to_nearest_hour(monkeypatch, delta_sec, expected):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=delta_sec))
    assert detect_server_offset_hours() == expected


def test_detect_offset_closed_market_returns_fallback(monkeypatch):
    _pin_weekday(monkeypatch)
    # tick ~30h in the future => last quote is from Friday's close; the delta is
    # downtime, not the offset, so fall back instead of trusting it.
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=30 * 3600))
    assert detect_server_offset_hours(fallback=3.0) == 3.0


def test_detect_offset_implausible_delta_returns_fallback(monkeypatch):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=30 * 3600))
    assert detect_server_offset_hours(fallback=2.0) == 2.0


def test_detect_offset_symbol_select_failure_returns_fallback(monkeypatch):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801, fail_select=True))
    assert detect_server_offset_hours(fallback=3.0) == 3.0


def test_detect_offset_no_tick_returns_fallback(monkeypatch):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=None))
    assert detect_server_offset_hours(fallback=0.0) == 0.0


def test_detect_offset_weekend_returns_fallback(monkeypatch):
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801))
    monkeypatch.setattr("data.mt5_provider._is_weekend_utc", lambda: True)
    assert detect_server_offset_hours(fallback=3.0) == 3.0


def test_resolve_numeric_value_passthrough():
    assert resolve_server_offset({"server_time_offset_hours": 2.0}) == 2.0
    assert resolve_server_offset({"server_time_offset_hours": 0}) == 0.0


def test_resolve_auto_detects_from_live_tick(monkeypatch):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801))
    cfg = {"server_time_offset_hours": "auto", "server_time_offset_hours_fallback": 3.0}
    assert resolve_server_offset(cfg) == 3.0


def test_resolve_auto_uses_fallback_when_market_closed(monkeypatch):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=30 * 3600))
    cfg = {"server_time_offset_hours": "auto", "server_time_offset_hours_fallback": 3.0}
    assert resolve_server_offset(cfg) == 3.0


def test_resolve_auto_without_fallback_defaults_zero(monkeypatch):
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=None))
    assert resolve_server_offset({"server_time_offset_hours": "auto"}) == 0.0


def test_resolve_missing_key_returns_zero():
    assert resolve_server_offset({}) == 0.0
    assert resolve_server_offset(None) == 0.0


# ---------------------------------------------------------------------------
# DST transition simulation (EEST -> EET, weekend of Sun 2026-10-25)
# ---------------------------------------------------------------------------


class _PinnedDatetime:
    """Stub for data.mt5_provider.datetime: returns a pinned aware UTC now.

    Drives the REAL ``_is_weekend_utc`` logic (weekday of now) instead of a
    lambda, so the transition tests exercise the actual weekend guard.
    """
    _now = None

    @classmethod
    def now(cls, tz=None):
        if cls._now is None:
            raise RuntimeError("_PinnedDatetime._now not set")
        return cls._now if tz is None else cls._now.astimezone(tz)


_DST_SUN = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)  # Sunday, market closed
_DST_MON = datetime(2026, 10, 26, 8, 0, 0, tzinfo=timezone.utc)   # Monday, market open


def _pin_clock(monkeypatch, pinned_utc: datetime) -> float:
    """Pin module clock to a concrete UTC datetime and return its epoch."""
    epoch = float(int(pinned_utc.timestamp()))
    _PinnedDatetime._now = pinned_utc
    monkeypatch.setattr("data.mt5_provider.datetime", _PinnedDatetime)
    monkeypatch.setattr("data.mt5_provider.time.time", lambda: epoch)
    return epoch


def test_dst_transition_sunday_start_eet_tick_uses_fallback(monkeypatch):
    """Sunday start during the EEST->EET weekend.

    The server has ALREADY flipped to EET (tick delta 2h), but the market is
    closed: the tick is stale downtime, not the offset. The weekend guard must
    reject it and return the configured fallback (3h = last known EEST value),
    NOT 2.0 — otherwise every Sunday would silently adopt a bogus offset.
    """
    epoch = _pin_clock(monkeypatch, _DST_SUN)
    monkeypatch.setattr("data.mt5_provider.mt5",
                        _FakeMt5(tick_delta_sec=7201, base_time=epoch))
    assert detect_server_offset_hours(fallback=3.0) == 3.0


def test_dst_transition_monday_fresh_tick_overrides_fallback(monkeypatch):
    """Monday open: the fresh EET tick (2h) overrides the 3h fallback."""
    epoch = _pin_clock(monkeypatch, _DST_MON)
    monkeypatch.setattr("data.mt5_provider.mt5",
                        _FakeMt5(tick_delta_sec=7201, base_time=epoch))
    assert detect_server_offset_hours(fallback=3.0) == 2.0


def test_dst_transition_resolve_auto_sequence(monkeypatch):
    """Full config-level sequence across the transition weekend.

    Same process, same ``auto`` config: Sunday's re-resolution returns the
    fallback (weekend guard wins over the 2h EET tick), Monday's fresh
    measurement overrides it with 2.0 — exactly the self-heal the resolver
    promises for consumers that re-resolve per poll (e.g. the pipeline).
    """
    cfg = {"server_time_offset_hours": "auto",
           "server_time_offset_hours_fallback": 3.0}

    epoch = _pin_clock(monkeypatch, _DST_SUN)
    monkeypatch.setattr("data.mt5_provider.mt5",
                        _FakeMt5(tick_delta_sec=7201, base_time=epoch))
    assert resolve_server_offset(cfg) == 3.0   # Sunday: fallback, not 2h

    epoch = _pin_clock(monkeypatch, _DST_MON)
    monkeypatch.setattr("data.mt5_provider.mt5",
                        _FakeMt5(tick_delta_sec=7201, base_time=epoch))
    assert resolve_server_offset(cfg) == 2.0   # Monday: fresh tick wins


# ---------------------------------------------------------------------------
# Detailed resolvers (offset + provenance info) — the mode/reason the trader
# and pipeline now log at startup, so log forensics show the WHY, not just
# the value. The thin wrappers above must stay behaviour-identical.
# ---------------------------------------------------------------------------

from data.mt5_provider import (
    detect_server_offset_hours_detailed,
    resolve_server_offset_detailed,
)


def test_detect_detailed_fresh_tick_reports_detected(monkeypatch):
    """Live tick -> mode='detected', reason carries the measured delta."""
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801))
    offset, info = detect_server_offset_hours_detailed(fallback=3.0)
    assert offset == 3.0
    assert info["mode"] == "detected"
    assert "tick_delta_hours" in info["reason"]
    assert info["delta_hours"] == pytest.approx(10801 / 3600.0)


def test_detect_detailed_weekend_reports_fallback(monkeypatch):
    """Weekend guard -> mode='fallback', reason names the closed market."""
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801))
    monkeypatch.setattr("data.mt5_provider._is_weekend_utc", lambda: True)
    offset, info = detect_server_offset_hours_detailed(fallback=3.0)
    assert offset == 3.0
    assert info["mode"] == "fallback"
    assert info["reason"] == "weekend_market_closed"


def test_detect_detailed_implausible_delta_reports_fallback(monkeypatch):
    """Stale tick (market closed, not weekend guard) -> fallback with reason."""
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=30 * 3600))
    offset, info = detect_server_offset_hours_detailed(fallback=2.0)
    assert offset == 2.0
    assert info["mode"] == "fallback"
    assert info["reason"].startswith("implausible_delta_hours=")


def test_detect_detailed_mt5_unavailable_reports_fallback(monkeypatch):
    """Terminal unreachable -> fallback with the underlying exception reason."""
    class _BrokenMt5:
        def initialize(self):
            raise RuntimeError("terminal down")
    monkeypatch.setattr("data.mt5_provider.mt5", _BrokenMt5())
    offset, info = detect_server_offset_hours_detailed(fallback=3.0)
    assert offset == 3.0
    assert info["mode"] == "fallback"
    assert info["reason"].startswith("mt5_unavailable:")
    assert "terminal down" in info["reason"]


def test_resolve_detailed_numeric_reports_explicit():
    """Numeric config -> mode='explicit', reason shows the config value."""
    offset, info = resolve_server_offset_detailed({"server_time_offset_hours": 2.0})
    assert offset == 2.0
    assert info["mode"] == "explicit"
    assert "config=2.0" in info["reason"]


def test_resolve_detailed_auto_carries_detection_info(monkeypatch):
    """auto -> the underlying detection info is surfaced, plus the fallback used."""
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801))
    cfg = {"server_time_offset_hours": "auto",
           "server_time_offset_hours_fallback": 3.0}
    offset, info = resolve_server_offset_detailed(cfg)
    assert offset == 3.0
    assert info["mode"] == "detected"
    assert info["fallback"] == 3.0


def test_resolve_detailed_invalid_config_reports_invalid():
    """Unparseable config -> mode='invalid' (never silently trusts garbage)."""
    offset, info = resolve_server_offset_detailed({"server_time_offset_hours": "banana"})
    assert offset == 0.0
    assert info["mode"] == "invalid"
    assert "banana" in info["reason"]


def test_detailed_and_thin_wrappers_agree(monkeypatch):
    """Wrappers must return the SAME number the detailed path decided."""
    _pin_weekday(monkeypatch)
    monkeypatch.setattr("data.mt5_provider.mt5", _FakeMt5(tick_delta_sec=10801))
    detailed, info = detect_server_offset_hours_detailed(fallback=3.0)
    assert detect_server_offset_hours(fallback=3.0) == detailed
    assert info["mode"] == "detected"
