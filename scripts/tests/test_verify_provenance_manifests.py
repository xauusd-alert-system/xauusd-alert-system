"""verify_provenance_manifests: CI sweep over ALL frozen provenance manifests.

Covers the happy path (every manifest verified -> exit 0 + CSV row), the
fail-closed path on a tampered data_hash, the manifest self-hash seal (a
cosmetic edit the DB checks cannot see must still fail), and the main()
orchestration exit codes / CSV report used by CI.
"""
from __future__ import annotations

import csv
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import scripts.verify_provenance_manifests as vpm
from data.provenance import (
    build_provenance_manifest,
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
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "XAUUSD_M5_fxpro.json")
    write_provenance_manifest(path, manifest)
    return path


def _rewrite(path: str, mutate) -> None:
    manifest = json.load(open(path, encoding="utf-8"))
    mutate(manifest)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)


def test_verify_one_happy_path(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    path = _build_manifest(db, str(tmp_path / "manifests"))

    row = vpm.verify_one(db, path)

    assert row["verified"] is True
    assert row["reason"] == "ok"
    assert row["asset_key"] == "XAUUSD" and row["timeframe"] == "M5"
    assert row["manifest_hash_ok"] is True
    assert row["data_hash_match"] is True
    assert row["window_match"] is True
    assert row["count_match"] is True
    assert row["recomputed_data_hash"] == row["stored_data_hash"]


def test_verify_one_tampered_data_hash_fails(tmp_path):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    path = _build_manifest(db, str(tmp_path / "manifests"))

    _rewrite(path, lambda m: m.update({"data_hash": "0" * 64}))

    row = vpm.verify_one(db, path)

    assert row["verified"] is False
    assert "data_hash mismatch" in row["reason"]
    assert row["data_hash_match"] is False


def test_verify_one_cosmetic_edit_breaks_self_hash_seal(tmp_path):
    """A field the DB checks cannot see (export_time_utc) must still fail:
    the manifest_hash is the byte-level immutability seal over the file."""
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    path = _build_manifest(db, str(tmp_path / "manifests"))

    _rewrite(path, lambda m: m.update({"export_time_utc": "2099-01-01T00:00:00+00:00"}))

    row = vpm.verify_one(db, path)

    assert row["verified"] is False
    assert "self-hash mismatch" in row["reason"]
    assert row["manifest_hash_ok"] is False
    # content still matches the DB — the failure is purely the seal
    assert row["data_hash_match"] is True
    assert row["window_match"] is True and row["count_match"] is True


def test_verify_one_missing_db_table_fails(tmp_path):
    """Manifest frozen against a seeded DB, but the target DB has no candles
    for that (asset, timeframe) at all — never silently OK."""
    seeded = str(tmp_path / "seeded.sqlite")
    _seed_db(seeded)
    path = _build_manifest(seeded, str(tmp_path / "manifests"))

    empty_db = str(tmp_path / "empty.sqlite")  # no M5 table
    row = vpm.verify_one(empty_db, path)

    assert row["verified"] is False
    assert row["reason"]  # db read or count mismatch — never silently OK


def test_main_exit_zero_and_csv(tmp_path, monkeypatch):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    out = str(tmp_path / "manifests")
    _build_manifest(db, out)
    report = str(tmp_path / "audit.csv")

    monkeypatch.setattr("scripts.verify_provenance_manifests.load_config", lambda: {})
    monkeypatch.setattr(sys, "argv", [
        "verify_provenance_manifests.py",
        "--db", db, "--manifest-dir", out, "--out", report,
    ])

    assert vpm.main() == 0

    with open(report, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["asset_key"] == "XAUUSD"
    assert rows[0]["verified"] == "True"
    assert rows[0]["timeframe"] == "M5"


def test_main_exit_one_on_any_failure(tmp_path, monkeypatch):
    db = str(tmp_path / "db.sqlite")
    _seed_db(db)
    out = str(tmp_path / "manifests")
    path = _build_manifest(db, out)
    _rewrite(path, lambda m: m.update({"candle_count": 999}))
    report = str(tmp_path / "audit.csv")

    monkeypatch.setattr("scripts.verify_provenance_manifests.load_config", lambda: {})
    monkeypatch.setattr(sys, "argv", [
        "verify_provenance_manifests.py",
        "--db", db, "--manifest-dir", out, "--out", report,
    ])

    assert vpm.main() == 1

    with open(report, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["verified"] == "False"


def test_main_no_manifests_fails_closed(monkeypatch, tmp_path):
    empty_dir = str(tmp_path / "empty")
    os.makedirs(empty_dir)
    monkeypatch.setattr("scripts.verify_provenance_manifests.load_config", lambda: {})
    monkeypatch.setattr(sys, "argv", [
        "verify_provenance_manifests.py",
        "--manifest-dir", empty_dir, "--out", str(tmp_path / "audit.csv"),
    ])
    assert vpm.main() == 1

    # --allow-empty explicitly opts out of the fail-closed default.
    monkeypatch.setattr(sys, "argv", [
        "verify_provenance_manifests.py",
        "--manifest-dir", empty_dir, "--out", str(tmp_path / "audit.csv"),
        "--allow-empty",
    ])
    assert vpm.main() == 0
