"""Tests for provenance/api.py — single GET + bulk audit (ТЗ 8.7 / P2-3)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provenance.api import provenance_router
from provenance.store import ProvenanceStore
from provenance.tests.test_spec import NOW_MS, make_record


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "prov.sqlite")
    app = FastAPI()
    app.include_router(provenance_router(db_path))
    return TestClient(app), ProvenanceStore(db_path)


def test_single_get_200_with_record(client):
    client, store = client
    store.save(make_record())
    response = client.get("/api/provenance/TG-PROV-1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["record"]["group_id"] == "TG-PROV-1"
    assert payload["verification"]["complete"] is True
    assert payload["verification"]["hash_ok"] is True


def test_single_get_404_without_record(client):
    client, _store = client
    response = client.get("/api/provenance/TG-MISSING")
    assert response.status_code == 404


def test_bulk_audit_aggregates(client):
    """3 records: 2 complete + 1 with a missing field (P2-3 counters)."""
    client, store = client
    store.save(make_record(group_id="TG-1", signal_id="SGL-1"))
    store.save(make_record(group_id="TG-2", signal_id="SGL-2"))
    store.save(make_record(group_id="TG-3", signal_id="SGL-3", broker_snapshot={}))  # incomplete lineage
    response = client.get(
        "/api/provenance/bulk",
        params={"from": NOW_MS - 1000, "to": NOW_MS + 1000},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_groups"] == 3
    assert payload["complete_lineage_count"] == 2
    assert payload["incomplete_lineage_count"] == 1
    assert payload["missing_fields_counter"] == {"broker_snapshot": 1}
    assert payload["avg_time_to_execution_ms"] is None


def test_bulk_audit_avg_time_to_execution(client):
    client, store = client
    store.save(make_record(group_id="TG-E1", signal_id="SGL-E1", executed_at_utc_ms=NOW_MS + 500))
    store.save(make_record(group_id="TG-E2", signal_id="SGL-E2", executed_at_utc_ms=NOW_MS + 1500))
    payload = client.get(
        "/api/provenance/bulk",
        params={"from": NOW_MS - 1000, "to": NOW_MS + 2000},
    ).json()
    assert payload["avg_time_to_execution_ms"] == 1000.0


def test_bulk_empty_range_and_validation(client):
    client, _store = client
    payload = client.get(
        "/api/provenance/bulk",
        params={"from": NOW_MS, "to": NOW_MS + 1000},
    ).json()
    assert payload["total_groups"] == 0
    assert payload["complete_lineage_count"] == 0
    assert (
        client.get(
            "/api/provenance/bulk",
            params={"from": NOW_MS + 1000, "to": NOW_MS},
        ).status_code
        == 422
    )


def test_bulk_missing_params_422(client):
    client, _store = client
    assert client.get("/api/provenance/bulk").status_code == 422


def test_bulk_audit_cli(tmp_path, capsys):
    """scripts/audit_provenance.py mirrors the bulk aggregates."""
    from scripts.audit_provenance import main

    db_path = str(tmp_path / "prov.sqlite")
    store = ProvenanceStore(db_path)
    store.save(make_record(group_id="TG-C1", signal_id="SGL-C1"))
    store.save(make_record(group_id="TG-C2", signal_id="SGL-C2", cost_snapshot={}))
    code = main(["--from", str(NOW_MS - 1000), "--to", str(NOW_MS + 1000), "--db", db_path])
    out = capsys.readouterr().out
    assert code == 0
    assert "total_groups          : 2" in out
    assert "complete_lineage_count: 1" in out
    assert "- cost_snapshot: 1" in out

    code_json = main(["--from", str(NOW_MS - 1000), "--to", str(NOW_MS + 1000), "--db", db_path, "--json"])
    assert code_json == 0
    assert '"total_groups": 2' in capsys.readouterr().out

    code_strict = main(["--from", str(NOW_MS - 1000), "--to", str(NOW_MS + 1000), "--db", db_path, "--strict"])
    assert code_strict == 1
