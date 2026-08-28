"""Unit tests for realtime/data_envelope.py (freshness contract, spec §6.3)."""
from __future__ import annotations

from realtime.data_envelope import (
    FRESH_AFTER_MS,
    STALE_AFTER_MS,
    VALID_STATUSES,
    error_payload,
    freshness_fields,
    freshness_status,
    stamp,
)


def test_freshness_status_boundaries():
    now = 1_000_000
    # never produced -> waiting
    assert freshness_status(None, now) == "waiting"
    # <= 5s -> fresh
    assert freshness_status(now, now) == "fresh"
    assert freshness_status(now - FRESH_AFTER_MS, now) == "fresh"
    assert freshness_status(now - FRESH_AFTER_MS - 1, now) == "stale"
    # 5..60s -> stale
    assert freshness_status(now - 10_000, now) == "stale"
    assert freshness_status(now - STALE_AFTER_MS, now) == "stale"
    # > 60s -> offline
    assert freshness_status(now - STALE_AFTER_MS - 1, now) == "offline"
    assert freshness_status(now - 3_600_000, now) == "offline"
    # clock skew (future timestamp) never offline
    assert freshness_status(now + 5_000, now) == "fresh"


def test_freshness_status_custom_thresholds():
    now = 1_000_000
    assert freshness_status(now - 60_000, now, stale_after_ms=120_000) == "stale"
    assert freshness_status(now - 61_000, now, stale_after_ms=60_000) == "offline"


def test_freshness_fields_shape():
    now = 1_000_000
    fields = freshness_fields(now - 10_000, source="mt5_account", mode="live", now=now)
    assert fields["freshness_status"] == "stale"
    assert fields["as_of_utc_ms"] == now - 10_000
    assert fields["ingest_lag_ms"] == 10_000
    assert fields["last_successful_at_utc_ms"] == now - 10_000
    assert fields["coverage"] is None
    empty = freshness_fields(None, source="x", mode="y", now=now)
    assert empty["freshness_status"] == "waiting"
    assert empty["as_of_utc_ms"] is None
    assert empty["ingest_lag_ms"] is None


def test_stamp_preserves_payload_and_adds_fields():
    now = 1_000_000
    payload = {"available": True, "balance": 123.0}
    out = stamp(payload, last_activity_ms=now, source="mt5_account", mode="live",
                coverage=0.95, now=now)
    assert out["balance"] == 123.0
    assert out["freshness_status"] == "fresh"
    assert out["coverage"] == 0.95
    assert out["source"] == "mt5_account"
    # original dict untouched
    assert "freshness_status" not in payload


def test_error_payload_never_carries_stale_value():
    now = 1_000_000
    err = error_payload(source="mt5_history_deals", mode="live", reason="boom", now=now)
    assert err["available"] is False
    assert err["freshness_status"] == "error"
    assert err["as_of_utc_ms"] is None
    assert err["reason"] == "boom"
    assert "balance" not in err and "pnl" not in err


def test_valid_statuses_cover_spec():
    assert VALID_STATUSES == {"fresh", "stale", "offline", "waiting", "error"}
