"""FastAPI router for provenance (ТЗ 8.7).

Endpoints:

* ``GET /api/provenance/{group_id}`` — one record + verification result;
  404 when absent (no fabrication, §38 semantics).
* ``GET /api/provenance/bulk?from=&to=`` — bulk lineage audit (P2-3):
  ``total_groups``, ``complete_lineage_count``, ``missing_fields_counter``,
  ``avg_time_to_execution_ms``.

The router is factory-built (``provenance_router(db_path, cfg=None)``) so
tests can wire a temp DB without touching the global app. It is mounted in
``realtime/app.py``; auth arrives in Phase 4 — the endpoint is read-only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from provenance.store import ProvenanceStore
from provenance.verifier import verify_record


def provenance_router(db_path: str, cfg: dict[str, Any] | None = None) -> APIRouter:
    """Build a provenance APIRouter bound to one ProvenanceStore path."""
    store = ProvenanceStore(db_path)
    router = APIRouter(prefix="/api/provenance", tags=["provenance"])

    # NOTE: /bulk MUST be registered BEFORE /{group_id} — otherwise FastAPI
    # matches "bulk" as a group_id and the audit endpoint 404s.
    @router.get("/bulk")
    def bulk_audit(
        frm: int = Query(..., alias="from", description="range start (utc ms, inclusive)"),
        to: int = Query(..., description="range end (utc ms, inclusive)"),
    ) -> dict[str, Any]:
        if frm > to:
            raise HTTPException(status_code=422, detail="'from' must be <= 'to'")
        records = store.get_range(frm, to)
        total = len(records)
        missing_counter: Counter[str] = Counter()
        complete = 0
        exec_deltas: list[int] = []
        for record in records:
            result = verify_record(record, store=store, cfg=cfg)
            if result.complete:
                complete += 1
            else:
                for key in result.missing_fields:
                    missing_counter[key] += 1
            if record.executed_at_utc_ms is not None:
                exec_deltas.append(int(record.executed_at_utc_ms) - int(record.as_of_utc_ms))
        avg_exec = round(sum(exec_deltas) / len(exec_deltas), 3) if exec_deltas else None
        return {
            "from_ts": frm,
            "to_ts": to,
            "total_groups": total,
            "complete_lineage_count": complete,
            "incomplete_lineage_count": total - complete,
            "missing_fields_counter": dict(sorted(missing_counter.items())),
            "avg_time_to_execution_ms": avg_exec,
        }

    @router.get("/{group_id}")
    def get_provenance(group_id: str) -> dict[str, Any]:
        record = store.get(group_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "provenance record not found",
                    "group_id": group_id,
                },
            )
        result = verify_record(record, store=store, cfg=cfg)
        return {
            "record": record.model_dump(mode="json"),
            "verification": result.to_dict(),
        }

    return router
