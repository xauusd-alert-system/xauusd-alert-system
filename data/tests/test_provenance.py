"""Tests for data/provenance.py (immutable raw-data provenance manifest)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from data.provenance import (
    build_provenance_manifest,
    compute_data_hash,
    gap_audit,
    provenance_gate,
    verify_provenance_manifest,
    write_provenance_manifest,
)
from data.storage import init_schema, upsert_candles

SESSIONS = {"asia": {"start": 0, "end": 8}, "london": {"start": 8, "end": 13},
            "newyork": {"start": 13, "end": 22}}


def _frame(start_ts: int, interval: int, n: int, *, drop: set[int] | None = None) -> pd.DataFrame:
    drop = drop or set()
    rows = []
    for i in range(n):
        ts = start_ts + i * interval
        if ts in drop:
            continue
        rows.append({
            "timestamp_utc": ts, "open": 1.0, "high": 1.1, "low": 0.9,
            "close": 1.05, "volume": 100.0, "session": "london",
            "spread": 25.0, "real_volume": 90.0,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "market.sqlite")
    init_schema(path, ["M15"])
    return path


def _monday_0900() -> int:
    # 2024-01-01 09:00 UTC was a Monday
    return int(pd.Timestamp("2024-01-01 09:00", tz="UTC").timestamp())


def test_build_manifest_content(db):
    df = _frame(_monday_0900(), 900, 100)
    upsert_candles(db, "M15", "XAUUSD", df)
    manifest = build_provenance_manifest(
        db, "M15", "XAUUSD", broker="FxPro", broker_symbol="GOLD",
        terminal_build="4680", sessions_config=SESSIONS,
    )
    assert manifest["asset_key"] == "XAUUSD"
    assert manifest["broker_symbol"] == "GOLD"
    assert manifest["broker"] == "FxPro"
    assert manifest["candle_count"] == 100
    assert manifest["interval_seconds"] == 900
    assert manifest["source_window_utc"]["start_ts"] == _monday_0900()
    assert len(manifest["manifest_hash"]) == 64
    assert manifest["gap_audit"]["present_bars"] == 100
    assert manifest["gap_audit"]["missing_bars"] == 0
    assert manifest["data_hash"] == compute_data_hash(df)


def test_write_is_atomic_and_immutable(db, tmp_path):
    df = _frame(_monday_0900(), 900, 50)
    upsert_candles(db, "M15", "XAUUSD", df)
    manifest = build_provenance_manifest(db, "M15", "XAUUSD", broker="B",
                                         sessions_config=SESSIONS)
    path = str(tmp_path / "manifest.json")
    write_provenance_manifest(path, manifest)
    write_provenance_manifest(path, manifest)  # identical rewrite is fine
    altered = dict(manifest)
    altered["broker"] = "OTHER"
    with pytest.raises(RuntimeError, match="already exists with different content"):
        write_provenance_manifest(path, altered)


def test_verify_detects_data_change(db, tmp_path):
    df = _frame(_monday_0900(), 900, 60)
    upsert_candles(db, "M15", "XAUUSD", df)
    manifest = build_provenance_manifest(db, "M15", "XAUUSD", broker="B",
                                         sessions_config=SESSIONS)
    path = str(tmp_path / "manifest.json")
    write_provenance_manifest(path, manifest)

    assert verify_provenance_manifest(db, "M15", "XAUUSD", path)["verified"] is True

    # append one extra candle -> data hash / window change -> fail closed
    extra = _frame(_monday_0900() + 60 * 900, 900, 1)
    upsert_candles(db, "M15", "XAUUSD", extra)
    with pytest.raises(RuntimeError, match="data_hash mismatch|source window mismatch"):
        verify_provenance_manifest(db, "M15", "XAUUSD", path)


def test_verify_rejects_wrong_asset_or_timeframe(db, tmp_path):
    df = _frame(_monday_0900(), 900, 10)
    upsert_candles(db, "M15", "XAUUSD", df)
    manifest = build_provenance_manifest(db, "M15", "XAUUSD", broker="B")
    path = str(tmp_path / "manifest.json")
    write_provenance_manifest(path, manifest)
    with pytest.raises(RuntimeError, match="asset_key"):
        verify_provenance_manifest(db, "M15", "BTCUSD", path)
    with pytest.raises(RuntimeError, match="timeframe"):
        verify_provenance_manifest(db, "M5", "XAUUSD", path)


def test_gap_audit_reports_missing_and_weekend(db):
    start = _monday_0900()
    # drop bars 5..8 (a 1-hour hole) -> 4 missing M15 bars in one gap
    dropped = {start + i * 900 for i in range(5, 9)}
    df = _frame(start, 900, 100, drop=dropped)
    upsert_candles(db, "M15", "XAUUSD", df)
    audit = gap_audit(df, "M15", SESSIONS)
    assert audit["missing_bars"] == 4
    assert audit["gap_count"] == 1
    gap = audit["gaps"][0]
    assert gap["missing_bars"] == 4
    assert gap["spans_weekend"] is False
    assert audit["per_year"]["2024"]["coverage"] == pytest.approx(96 / 100, abs=1e-6)


def test_gap_audit_empty_frame():
    audit = gap_audit(pd.DataFrame(), "M15", SESSIONS)
    assert audit["present_bars"] == 0
    assert audit["coverage"] is None


def test_provenance_gate_config_driven(db, tmp_path):
    df = _frame(_monday_0900(), 900, 20)
    upsert_candles(db, "M15", "XAUUSD", df)
    manifest = build_provenance_manifest(db, "M15", "XAUUSD", broker="B")
    path = str(tmp_path / "manifest.json")
    write_provenance_manifest(path, manifest)

    # not required -> no-op
    assert provenance_gate({}, db, "M15", "XAUUSD") == {
        "verified": False, "required": False, "reason": "not required"}

    cfg = {"validation": {"require_provenance_manifest": True,
                          "provenance_manifest_path": path}}
    result = provenance_gate(cfg, db, "M15", "XAUUSD")
    assert result["verified"] is True and result["required"] is True

    # required but missing -> fail closed
    cfg_missing = {"validation": {"require_provenance_manifest": True,
                                  "provenance_manifest_path": str(tmp_path / "nope.json")}}
    with pytest.raises(RuntimeError, match="not found"):
        provenance_gate(cfg_missing, db, "M15", "XAUUSD")
