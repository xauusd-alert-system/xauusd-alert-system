"""Tests for data/intent_ledger.py (immutable SignalIntent store)."""

from __future__ import annotations

import sqlite3

import pytest

from contracts.execution_contracts import build_signal_intent
from data.intent_ledger import append_signal_intent, read_signal_intent


def _intent(intent_id: str = "ab" * 16):
    return build_signal_intent(
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        side="long",
        requested_volume=0.1,
        entry_price=4250.0,
        sl_price=4240.0,
        tp_price=4270.0,
        model_version="v3",
        config_hash="c" * 64,
        mode="demo_systematic",
        magic_number=777111,
        signal_id="sig-1",
        created_at_utc_ms=1_700_000_000_000,
        intent_id=intent_id,
    )


def test_append_and_read_roundtrip(tmp_path):
    path = str(tmp_path / "intents.sqlite")
    intent = _intent()
    assert append_signal_intent(path, intent) == intent.intent_id
    row = read_signal_intent(path, intent.intent_id)
    assert row is not None
    assert row["asset_key"] == "XAUUSD"
    assert row["broker_symbol"] == "GOLD"
    assert row["side"] == "long"
    assert row["magic_number"] == 777111
    assert row["mode"] == "demo_systematic"


def test_append_is_idempotent(tmp_path):
    path = str(tmp_path / "intents2.sqlite")
    intent = _intent()
    append_signal_intent(path, intent)
    append_signal_intent(path, intent)  # same intent_id -> no second row
    conn = sqlite3.connect(path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM ledger_intents").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_append_only_triggers(tmp_path):
    path = str(tmp_path / "intents3.sqlite")
    append_signal_intent(path, _intent())
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ledger_intents SET side='short'")
            conn.commit()
    finally:
        conn.close()


def test_read_missing_returns_none(tmp_path):
    path = str(tmp_path / "intents4.sqlite")
    assert read_signal_intent(path, "missing") is None
