"""TZ_BOOKS T-16: the SQLite signal bridge between Python and the EA."""
from __future__ import annotations

import sqlite3
import time

import pytest

from execution.signal_bridge import (
    SCHEMA_VERSION,
    STATUSES,
    SignalBridgeWriter,
    SignalIntent,
)


@pytest.fixture()
def writer(tmp_path):
    w = SignalBridgeWriter(str(tmp_path / "bridge.sqlite"))
    w.connect()
    yield w
    w.close()


def _intent(intent_id: str = "sig-1", **kwargs) -> SignalIntent:
    base = dict(intent_id=intent_id, asset="XAUUSD", direction=1,
                probability=0.73)
    base.update(kwargs)
    return SignalIntent(**base)


def test_intent_validation():
    with pytest.raises(ValueError):
        SignalIntent("x", "XAUUSD", direction=0, probability=0.7)
    with pytest.raises(ValueError):
        SignalIntent("x", "XAUUSD", direction=1, probability=1.5)
    with pytest.raises(ValueError):
        SignalIntent("", "XAUUSD", direction=1, probability=0.7)


def test_write_and_pending(writer):
    writer.write_signal(_intent())
    writer.write_signal(_intent("sig-2", direction=-1, probability=0.81))
    pending = writer.pending_signals()
    assert [p["intent_id"] for p in pending] == ["sig-1", "sig-2"]
    assert all(p["status"] == "new" for p in pending)
    assert pending[0]["probability"] == pytest.approx(0.73)


def test_default_ttl_is_three_hours(writer):
    before = int(time.time())
    writer.write_signal(_intent())
    row = writer.pending_signals()[0]
    assert row["expires_at_utc"] - row["created_at_utc"] == 3 * 3600
    assert row["created_at_utc"] >= before


def test_retry_write_is_idempotent_and_keeps_status(writer):
    writer.write_signal(_intent())
    writer.mark("sig-1", "executed", comment="deal 123")

    # Python crashes and retries the SAME intent after the EA executed it
    writer.write_signal(_intent())
    conn = writer.connect()
    rows = conn.execute(
        "SELECT status, comment FROM ml_signals WHERE intent_id='sig-1'"
    ).fetchall()
    assert rows == [("executed", "deal 123")]


def test_mark_transitions_and_rejects_unknown_status(writer):
    writer.write_signal(_intent())
    assert writer.mark("sig-1", "consumed")
    assert writer.mark("sig-1", "executed", comment="filled")
    assert not writer.mark("missing-id", "executed")
    with pytest.raises(ValueError):
        writer.mark("sig-1", "teleported")
    assert set(STATUSES) >= {"new", "consumed", "executed", "skipped",
                             "failed", "expired"}


def test_expire_stale_only_touches_expired_new_rows(writer):
    now = int(time.time())
    writer.write_signal(_intent("old", expires_at_utc=now - 10))
    writer.write_signal(_intent("fresh", expires_at_utc=now + 3600))
    writer.write_signal(_intent("kept", expires_at_utc=now - 10))
    writer.mark("kept", "consumed")

    assert writer.expire_stale(now_utc=now) == 1
    pending = writer.pending_signals(now_utc=now)
    assert [p["intent_id"] for p in pending] == ["fresh"]
    # consumed rows are never flipped to expired
    conn = writer.connect()
    status = dict(conn.execute(
        "SELECT intent_id, status FROM ml_signals").fetchall())
    assert status == {"old": "expired", "fresh": "new", "kept": "consumed"}


def test_schema_version_pinned(writer):
    assert SCHEMA_VERSION == 1
    conn = writer.connect()
    row = conn.execute(
        "SELECT value FROM bridge_meta WHERE key='schema_version'").fetchone()
    assert row is not None and int(row[0]) == SCHEMA_VERSION


def test_wal_mode_enabled(writer):
    conn = writer.connect()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_ea_can_open_concurrently(writer):
    """The EA reads/writes the same file while the writer holds it open."""
    writer.write_signal(_intent())
    writer.write_signal(_intent("sig-2"))
    writer.mark("sig-1", "consumed", comment="picked by EA")

    # separate connection, like the MQL5 side
    ea = sqlite3.connect(writer.db_path)
    try:
        ea.execute("PRAGMA journal_mode=WAL")
        rows = ea.execute(
            "SELECT intent_id, status FROM ml_signals "
            "ORDER BY created_at_utc").fetchall()
        assert rows == [("sig-1", "consumed"), ("sig-2", "new")]
    finally:
        ea.close()
