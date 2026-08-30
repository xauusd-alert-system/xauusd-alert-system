import copy
import json
import sqlite3

import pandas as pd
import pytest

from config.loader import load_config
from data.paper_ledger import paper_accumulation_status, read_paper_events
from model.trainer import save_model
from paper.accumulator import (
    FrozenPaperAccumulator,
    create_frozen_manifest,
    load_frozen_manifest,
)
from scripts.run_live_forward_validation import main as validate_main


class FakePipeline:
    def __init__(self, bars, signals):
        self.bars = bars
        self.signals = signals
        self.i = 0

    def get_frame(self, n_candles=3, build_features=False):
        return pd.DataFrame([self.bars[self.i]])

    def generate_signal(self, n_candles=300):
        return self.signals[self.i]

    def advance(self):
        self.i += 1


def _manifest(tmp_path, min_trades=1):
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("validation", {})["locked_holdout"] = {
        "enabled": True,
        "start": "1970-01-01T00:01:40Z",
        "end": None,
    }
    model = tmp_path / "frozen.joblib"
    save_model(
        {"fake": True},
        ["rsi"],
        str(model),
        metadata={
            "bundle_schema_version": 2,
            "asset_key": "XAUUSD",
            "label_event": "traded",
            "timeframe": "M15",
            "data_period": {"end_timestamp_utc": 99},
        },
    )
    path = tmp_path / "manifest.json"
    manifest = create_frozen_manifest(
        cfg,
        asset_key="XAUUSD",
        variant="wide_trend_filtered",
        model_path=str(model),
        output_path=str(path),
        start_timestamp_utc=100,
        min_closed_trades=min_trades,
    )
    return manifest, path, model


def _signal(ts):
    return {
        "bias": "long",
        "confidence": 0.8,
        "step": 1.0,
        "timestamp_utc": ts,
        "regime": "trend_up",
        "session": "london",
        "entry_zone": [99.9, 100.1],
        "invalidation": 96.0,
        "targets": [101.0, 101.5, 104.0],
        "reasoning_summary": "frozen test",
        "features": {},
        "generated_at": "2026-08-08T00:00:00+00:00",
    }


def test_manifest_is_immutable_and_detects_model_change(tmp_path):
    manifest, path, model = _manifest(tmp_path)
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("validation", {})["locked_holdout"] = {
        "enabled": True,
        "start": "1970-01-01T00:01:40Z",
        "end": None,
    }
    same = create_frozen_manifest(
        cfg,
        asset_key="XAUUSD",
        variant="wide_trend_filtered",
        model_path=str(model),
        output_path=str(path),
        start_timestamp_utc=100,
        min_closed_trades=1,
    )
    assert same["manifest_sha256"] == manifest["manifest_sha256"]
    model.write_bytes(model.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="model SHA-256"):
        load_frozen_manifest(str(path))


def test_accumulator_is_idempotent_and_event_sourced(tmp_path):
    manifest, _, _ = _manifest(tmp_path)
    db = str(tmp_path / "paper.sqlite")
    bars = [
        {"timestamp_utc": 100, "open": 100, "high": 100.5, "low": 99.5, "close": 100},
        {"timestamp_utc": 200, "open": 100, "high": 100.4, "low": 99.8, "close": 100.2},
        {"timestamp_utc": 300, "open": 100.2, "high": 105, "low": 99.9, "close": 104},
    ]
    pipe = FakePipeline(bars, [_signal(100), _signal(200), _signal(300)])
    acc = FrozenPaperAccumulator(manifest, db)

    first = acc.process_once(pipe)
    assert first["signals"] == 1
    assert "total_pnl" not in first and "profit_factor" not in first
    count = len(read_paper_events(db, manifest["run_id"]))
    acc.process_once(pipe)
    assert len(read_paper_events(db, manifest["run_id"])) == count

    pipe.advance()
    second = acc.process_once(pipe)
    assert second["opened_trades"] == 1
    pipe.advance()
    final = acc.process_once(pipe)
    assert final["closed_trades"] == 1
    assert final["ready_for_one_time_validation"] is True

    events = read_paper_events(db, manifest["run_id"])
    assert list(events["event_type"]).count("open") == 1
    assert list(events["event_type"]).count("close") == 1
    close = events[events["event_type"] == "close"].iloc[0]["payload"]
    assert close["execution_cost_money"] > 0
    assert close["exit_reason"] in {"breakeven", "tp3_runner"}


def test_sqlite_triggers_reject_update_and_delete(tmp_path):
    manifest, _, _ = _manifest(tmp_path)
    db = str(tmp_path / "paper.sqlite")
    FrozenPaperAccumulator(manifest, db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE paper_runs SET variant='tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM paper_runs")


def test_validation_stays_sealed_then_allows_exactly_one_read(tmp_path, capsys):
    manifest, path, _ = _manifest(tmp_path, min_trades=2)
    db = str(tmp_path / "sealed.sqlite")
    FrozenPaperAccumulator(manifest, db)
    with pytest.raises(SystemExit, match="HOLD-OUT SEALED"):
        validate_main(["--manifest", str(path), "--paper-db", db])
    assert paper_accumulation_status(db, manifest["run_id"])["validation_reads"] == 0


def test_one_time_validation_burns_before_reporting(tmp_path, capsys):
    manifest, path, _ = _manifest(tmp_path, min_trades=1)
    db = str(tmp_path / "paper.sqlite")
    bars = [
        {"timestamp_utc": 100, "open": 100, "high": 100.5, "low": 99.5, "close": 100},
        {"timestamp_utc": 200, "open": 100, "high": 100.4, "low": 99.8, "close": 100.2},
        {"timestamp_utc": 300, "open": 100.2, "high": 105, "low": 99.9, "close": 104},
    ]
    pipe = FakePipeline(bars, [_signal(100), _signal(200), _signal(300)])
    acc = FrozenPaperAccumulator(manifest, db)
    for _ in bars:
        acc.process_once(pipe)
        if pipe.i < len(bars) - 1:
            pipe.advance()

    with pytest.raises(SystemExit, match="READY BUT SEALED"):
        validate_main(["--manifest", str(path), "--paper-db", db])
    assert paper_accumulation_status(db, manifest["run_id"])["validation_reads"] == 0

    out = tmp_path / "result.json"
    validate_main(["--manifest", str(path), "--paper-db", db, "--out", str(out), "--force"])
    result = json.loads(out.read_text())
    assert result["trial"]["n_trades"] == 1
    assert paper_accumulation_status(db, manifest["run_id"])["validation_reads"] == 1
    with pytest.raises(SystemExit, match="ALREADY READ"):
        validate_main(["--manifest", str(path), "--paper-db", db, "--force"])
