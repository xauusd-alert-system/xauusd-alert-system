"""Wave-0 provenance gate: fail-closed verification of the frozen manifest
before train_mt5 / run_backtest build any features.

Covers: directory resolution (config/provenance/<ASSET>_<TF>_fxpro.json),
file-path (legacy single-manifest) mode, and fail-closed behaviour on a
missing or tampered manifest.
"""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from data.provenance import (
    build_provenance_manifest,
    provenance_gate,
    resolve_manifest_path,
    write_provenance_manifest,
)
from data.storage import init_schema, upsert_candles

SESSIONS = {
    "asia": {"start": 0, "end": 8},
    "london": {"start": 8, "end": 13},
    "newyork": {"start": 13, "end": 22},
}


def _seed_db(db_path: str) -> None:
    """Tiny 3-bar XAUUSD M5 DB (true-UTC timestamps)."""
    init_schema(db_path, ["M5"])
    frame = pd.DataFrame([
        {"timestamp_utc": 1_800_000_000, "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.5, "volume": 10, "session": "london",
         "spread": 5, "real_volume": 200},
        {"timestamp_utc": 1_800_000_300, "open": 100.5, "high": 102.0,
         "low": 100.0, "close": 101.5, "volume": 12, "session": "london",
         "spread": 5, "real_volume": 220},
        {"timestamp_utc": 1_800_000_600, "open": 101.5, "high": 103.0,
         "low": 101.0, "close": 102.5, "volume": 15, "session": "newyork",
         "spread": 6, "real_volume": 240},
    ])
    upsert_candles(db_path, "M5", "XAUUSD", frame)


def _build_manifest(db_path: str, out_dir: str) -> str:
    manifest = build_provenance_manifest(
        db_path, "M5", "XAUUSD",
        broker="TestBroker", broker_symbol="GOLD",
        server_time_offset_hours=3.0,
        sessions_config=SESSIONS,
        extra={"note": "test"},
    )
    path = os.path.join(out_dir, "XAUUSD_M5_fxpro.json")
    write_provenance_manifest(path, manifest)
    return path


def test_resolve_manifest_path_directory(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    out = str(tmp_path / "manifests")
    os.makedirs(out)
    _build_manifest(db, out)

    resolved = resolve_manifest_path({"provenance_manifest_path": out}, "M5", "XAUUSD")
    assert resolved == os.path.join(out, "XAUUSD_M5_fxpro.json")

    # Wrong asset -> hard error, never silently skipped.
    with pytest.raises(RuntimeError, match="not found"):
        resolve_manifest_path({"provenance_manifest_path": out}, "M5", "BTCUSD")


def test_resolve_manifest_path_legacy_file(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    out = str(tmp_path / "manifests")
    os.makedirs(out)
    path = _build_manifest(db, out)

    assert resolve_manifest_path({"provenance_manifest_path": path}, "M5", "XAUUSD") == path


def test_gate_verifies_when_required(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    out = str(tmp_path / "manifests")
    os.makedirs(out)
    _build_manifest(db, out)

    cfg = {
        "validation": {
            "require_provenance_manifest": True,
            "provenance_manifest_path": out,
        }
    }
    result = provenance_gate(cfg, db, "M5", "XAUUSD")
    assert result["verified"] is True
    assert result["required"] is True
    assert result["manifest_path"].endswith("XAUUSD_M5_fxpro.json")


def test_gate_fail_closed_on_tampered_manifest(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    out = str(tmp_path / "manifests")
    os.makedirs(out)
    path = _build_manifest(db, out)

    # Tamper: the frozen candle count no longer matches the DB.
    manifest = json.load(open(path, encoding="utf-8"))
    manifest["candle_count"] = 999
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)

    cfg = {
        "validation": {
            "require_provenance_manifest": True,
            "provenance_manifest_path": out,
        }
    }
    with pytest.raises(RuntimeError, match="candle_count mismatch"):
        provenance_gate(cfg, db, "M5", "XAUUSD")


def test_gate_off_when_not_required(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    cfg = {"validation": {"require_provenance_manifest": False}}
    result = provenance_gate(cfg, db, "M5", "XAUUSD")
    assert result == {"verified": False, "required": False, "reason": "not required"}
