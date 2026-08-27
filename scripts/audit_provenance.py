"""Bulk provenance audit CLI (ТЗ 8.7 / P2-3).

Aggregates lineage completeness over a time window and prints the audit
summary (same aggregates as GET /api/provenance/bulk):

    python -m scripts.audit_provenance --from 1700000000000 --to 1799999999999 \
        [--db data/market_data_mt5.sqlite]

Exit code 0 always (audit reports; it does not gate) unless --strict is
given, in which case incomplete lineage exits 1.
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

from provenance.store import ProvenanceStore
from provenance.verifier import verify_record


def collect_audit(store: ProvenanceStore, from_ts: int, to_ts: int,
                  cfg: dict | None = None) -> dict:
    """Aggregate bulk audit over [from_ts, to_ts] (mirrors the bulk API)."""
    records = store.get_range(from_ts, to_ts)
    from collections import Counter

    missing_counter: Counter = Counter()
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
        "from_ts": from_ts,
        "to_ts": to_ts,
        "total_groups": len(records),
        "complete_lineage_count": complete,
        "incomplete_lineage_count": len(records) - complete,
        "missing_fields_counter": dict(sorted(missing_counter.items())),
        "avg_time_to_execution_ms": avg_exec,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.audit_provenance",
        description="Bulk provenance lineage audit (TZ 8.7 / P2-3).",
    )
    parser.add_argument("--from", dest="from_ts", type=int, required=True,
                        help="range start, utc ms (inclusive)")
    parser.add_argument("--to", dest="to_ts", type=int, required=True,
                        help="range end, utc ms (inclusive)")
    parser.add_argument("--db", default="data/market_data_mt5.sqlite",
                        help="provenance store database path")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when incomplete lineage exists")
    parser.add_argument("--json", action="store_true",
                        help="print the raw aggregate as JSON")
    args = parser.parse_args(argv)

    store = ProvenanceStore(args.db)
    summary = collect_audit(store, args.from_ts, args.to_ts)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Provenance audit [{args.from_ts} .. {args.to_ts}] db={args.db}")
        print(f"  total_groups          : {summary['total_groups']}")
        print(f"  complete_lineage_count: {summary['complete_lineage_count']}")
        print(f"  incomplete_lineage    : {summary['incomplete_lineage_count']}")
        missing = summary["missing_fields_counter"]
        if missing:
            print("  missing fields:")
            for key, count in missing.items():
                print(f"    - {key}: {count}")
        else:
            print("  missing fields        : (none)")
        avg = summary["avg_time_to_execution_ms"]
        print(f"  avg_time_to_execution : {avg if avg is not None else 'n/a'} ms")

    if args.strict and summary["incomplete_lineage_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
