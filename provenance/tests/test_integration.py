"""Integration tests: ProvenanceStore recording on group creation (ТЗ 8.7).

Config-gated (``provenance.store.enabled``) — pattern follows the Feature
Store integration tests (features/tests/test_feature_store.py).
"""
from __future__ import annotations

import os

from provenance.store import ProvenanceStore
from provenance.verifier import verify_record


def test_executor_records_provenance_when_enabled(tmp_path, monkeypatch):
    """create_group writes an audit record when provenance.store.enabled."""
    from execution.tests.test_trade_group_executor import BROKER, COST, make_spec
    from execution.trade_group_executor import PaperDriver, TradeGroupExecutor

    store_db = str(tmp_path / "prov_enabled.sqlite")
    monkeypatch.setattr(
        "execution.trade_group_executor.load_config",
        lambda: {"provenance": {"store": {"enabled": True,
                                          "db_path": store_db}}},
    )
    executor = TradeGroupExecutor(
        str(tmp_path / "exec.sqlite"), driver=PaperDriver(),
        cost=COST, broker=BROKER,
    )
    spec = make_spec()
    spec = spec.model_copy(update={"provenance": {
        "broker_snapshot_id": "BROKER:1", "cost_snapshot_id": "COST:1",
    }})
    executor.create_group(spec)

    store = ProvenanceStore(store_db)
    record = store.get(spec.group_id)
    assert record is not None
    assert record.group_id == spec.group_id
    assert record.signal_id == spec.signal_id
    result = verify_record(record)
    assert result.complete is True
    assert result.hash_ok is True


def test_executor_skips_when_disabled(tmp_path, monkeypatch):
    """Default config (enabled: false / unset) writes nothing (fail-open)."""
    from execution.tests.test_trade_group_executor import BROKER, COST, make_spec
    from execution.trade_group_executor import PaperDriver, TradeGroupExecutor

    store_db = str(tmp_path / "prov_disabled.sqlite")
    monkeypatch.setattr(
        "execution.trade_group_executor.load_config",
        lambda: {"provenance": {"store": {"enabled": False,
                                          "db_path": store_db}}},
    )
    executor = TradeGroupExecutor(
        str(tmp_path / "exec.sqlite"), driver=PaperDriver(),
        cost=COST, broker=BROKER,
    )
    executor.create_group(make_spec())
    assert not os.path.exists(store_db)

    # enabled flag absent entirely -> also skipped
    monkeypatch.setattr(
        "execution.trade_group_executor.load_config", lambda: {},
    )
    executor2 = TradeGroupExecutor(
        str(tmp_path / "exec2.sqlite"), driver=PaperDriver(),
        cost=COST, broker=BROKER,
    )
    executor2.create_group(make_spec(group_id="TG-DISABLED-2"))
    assert not os.path.exists(store_db)


def test_executor_fail_open_on_store_error(tmp_path, monkeypatch):
    """A broken store must never break the execution path (fail-open)."""
    from execution.tests.test_trade_group_executor import BROKER, COST, make_spec
    from execution.trade_group_executor import PaperDriver, TradeGroupExecutor

    monkeypatch.setattr(
        "execution.trade_group_executor.load_config",
        lambda: {"provenance": {"store": {
            "enabled": True,
            "db_path": str(tmp_path / "bad" / "dir.sqlite"),
        }}},
    )
    # Make ProvenanceStore construction explode -> swallowed by fail-open.
    def boom(_db_path):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(
        "execution.trade_group_executor.ProvenanceStore", boom,
    )
    from execution.trade_group import GroupState

    executor = TradeGroupExecutor(
        str(tmp_path / "exec.sqlite"), driver=PaperDriver(),
        cost=COST, broker=BROKER,
    )
    spec = make_spec()
    assert executor.create_group(spec) == GroupState.VALIDATED
