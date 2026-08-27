"""Tests for the Feature Store (ТЗ 8.3) and its pipeline integration."""
import copy
import os
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from features import FEATURES_SCHEMA_VERSION
from features.feature_store import (
    FeatureStore,
    compute_snapshot_id,
    features_hash,
)


FEATURES_A = {"ema_21": 4240.5, "atr": 4.25, "rsi_14": 61.0}
FEATURES_B = {"ema_21": 4241.0, "atr": 4.30, "rsi_14": 55.0}


@pytest.fixture()
def store(tmp_path):
    return FeatureStore(str(tmp_path / "feature_store.sqlite"))


# ---------------------------------------------------------------------------
# Core store contract
# ---------------------------------------------------------------------------


def test_compute_and_store_returns_deterministic_snapshot_id(store):
    """Same inputs (symbol|tf|bar_ts|version|features) -> same snapshot_id."""
    sid1, feats1 = store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        features=FEATURES_A,
    )
    sid2, feats2 = store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        features=FEATURES_A,
    )
    assert sid1 == sid2
    assert feats1 == FEATURES_A and feats2 == FEATURES_A

    expected = compute_snapshot_id(
        "XAUUSD", "M5", 1_700_000_000_000,
        FEATURES_SCHEMA_VERSION, features_hash(FEATURES_A),
    )
    assert sid1 == expected

    # A live compute_fn path yields the same id for the same values.
    sid3, _ = store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        compute_fn=lambda: dict(FEATURES_A),
    )
    assert sid3 == sid1


def test_get_latest_returns_most_recent(store):
    for ts in (1_700_000_000_000, 1_700_000_300_000, 1_700_000_600_000):
        store.compute_and_store(
            symbol="XAUUSD", timeframe="M5", bar_ts=ts,
            features={"atr": ts / 1e12},
        )
    latest = store.get_latest("XAUUSD", "M5")
    assert latest is not None
    assert latest["bar_ts_utc_ms"] == 1_700_000_600_000
    assert latest["features"] == {"atr": 1_700_000_600_000 / 1e12}


def test_get_range_filters_by_ts(store):
    for ts in (100, 200, 300, 400):
        store.compute_and_store(
            symbol="XAUUSD", timeframe="M15", bar_ts=ts, features={"t": ts}
        )
    rows = store.get_range("XAUUSD", "M15", from_ts=200, to_ts=300)
    assert [r["bar_ts_utc_ms"] for r in rows] == [200, 300]
    # Inclusive bounds and empty ranges behave as documented.
    assert [r["bar_ts_utc_ms"] for r in store.get_range("XAUUSD", "M15", 100, 100)] == [100]
    assert store.get_range("XAUUSD", "M15", 500, 900) == []


def test_duplicate_snapshot_upserts_or_ignores(store):
    """UNIQUE(symbol, tf, bar_ts, version): re-writing upserts, never duplicates."""
    sid1, _ = store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        features=FEATURES_A,
    )
    sid2, _ = store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        features=FEATURES_B,
    )
    conn = sqlite3.connect(store.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM feature_snapshots WHERE bar_ts_utc_ms = ?",
            (1_700_000_000_000,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert sid2 != sid1  # changed features -> new deterministic id wins
    assert store.get(sid2)["features"] == FEATURES_B
    assert store.get(sid1) is None  # old id no longer present after upsert


def test_migration_creates_tables(tmp_path):
    """Migration 002 (via data.migrate) creates the feature_snapshots table."""
    from data.migrate import apply_migrations

    db_path = str(tmp_path / "migrate_fs.sqlite")
    applied = apply_migrations(db_path)
    assert any(m.name == "feature_store" and m.version == 2 for m in applied)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(feature_snapshots)")
        }
    finally:
        conn.close()
    assert "feature_snapshots" in tables
    assert {"idx_feature_snapshots_lookup", "idx_feature_snapshots_ts"} <= indexes
    assert {
        "snapshot_id", "symbol", "timeframe", "bar_ts_utc_ms",
        "feature_set_version", "features_json", "computed_at_utc_ms",
    } <= columns
    # Idempotent: re-running applies nothing further.
    assert apply_migrations(db_path) == []


def test_version_isolation(store):
    """Different feature_set_version values never mix (UNIQUE includes version)."""
    store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        features=FEATURES_A, feature_set_version="v1",
    )
    store.compute_and_store(
        symbol="XAUUSD", timeframe="M5", bar_ts=1_700_000_000_000,
        features=FEATURES_B, feature_set_version="v2",
    )
    v1 = store.get_latest("XAUUSD", "M5", feature_set_version="v1")
    v2 = store.get_latest("XAUUSD", "M5", feature_set_version="v2")
    assert v1["features"] == FEATURES_A
    assert v2["features"] == FEATURES_B
    assert v1["snapshot_id"] != v2["snapshot_id"]
    assert v1["feature_set_version"] == "v1"
    assert v2["feature_set_version"] == "v2"
    # Unknown version -> nothing.
    assert store.get_latest("XAUUSD", "M5", feature_set_version="v999") is None


# ---------------------------------------------------------------------------
# Pipeline integration (config-gated, default off)
# ---------------------------------------------------------------------------


class _StubPredictor:
    """Minimal ModelPredictor stand-in: deterministic probs, 2 feature cols."""

    feature_cols = ["atr", "rsi_14"]

    def predict_single(self, row):
        return {"p_long": 0.7, "p_short": 0.2}


def _make_pipeline(cfg_overrides: dict | None = None):
    from realtime.pipeline import RealtimePipeline

    cfg = copy.deepcopy(load_config())
    if cfg_overrides:
        for section, values in cfg_overrides.items():
            cfg.setdefault(section, {}).update(values)
    pipeline = RealtimePipeline(cfg=cfg, model_path=None, data_mode="mock")
    pipeline._predictor = _StubPredictor()
    return pipeline, cfg


def test_pipeline_writes_snapshot_when_enabled(tmp_path):
    store_db = str(tmp_path / "fs_enabled.sqlite")
    pipeline, cfg = _make_pipeline(
        {"features": {"store": {"enabled": True, "db_path": store_db}}}
    )
    result = pipeline.generate_signal(n_candles=300)
    assert result["feature_snapshot_id"] is not None

    store = FeatureStore(store_db)
    latest = store.get_latest(pipeline.asset_key, pipeline.timeframe)
    assert latest is not None
    assert latest["snapshot_id"] == result["feature_snapshot_id"]
    # Snapshot values match the signal's published feature dict exactly.
    assert latest["features"] == result["features"]


def test_pipeline_skips_when_disabled(tmp_path):
    """Default config (features.store.enabled unset/false) writes nothing."""
    store_db = str(tmp_path / "fs_disabled.sqlite")
    pipeline, cfg = _make_pipeline(
        {"features": {"store": {"enabled": False, "db_path": store_db}}}
    )
    result = pipeline.generate_signal(n_candles=300)
    assert result["feature_snapshot_id"] is None
    assert not os.path.exists(store_db)

    # Sanity: even an existing store stays untouched.
    FeatureStore(store_db)
    from realtime.pipeline import RealtimePipeline as _RP  # noqa: F401 (import stability)

    store = FeatureStore(store_db)
    assert store.get_latest(pipeline.asset_key, pipeline.timeframe) is None


def test_pipeline_snapshot_id_is_deterministic_sha256_hex(tmp_path):
    """The published feature_snapshot_id is the store's sha256 hex snapshot id."""
    store_db = str(tmp_path / "fs_hex.sqlite")
    pipeline, _cfg = _make_pipeline(
        {"features": {"store": {"enabled": True, "db_path": store_db}}}
    )
    result = pipeline.generate_signal(n_candles=300)
    sid = result["feature_snapshot_id"]
    assert sid is None or (len(sid) == 64 and int(sid, 16) >= 0)
    if sid is not None:
        assert sid == FeatureStore(store_db).get(sid)["snapshot_id"]
