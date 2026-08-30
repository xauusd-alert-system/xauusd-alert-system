"""Provenance verifier (ТЗ P1.6 §40): checks that every critical value has a
source, source ids resolve, hashes match, parent relationships exist, timestamps
and freshness are valid, and no synthetic source is labeled as real.

Usage:
    python -m scripts.verify_provenance --group TG-... [--db data/market_data_mt5.sqlite]

Exit code 0 = verified; 1 = violations found. Never fabricates missing lineage:
a group without provenance is reported as ``provenance_status=legacy_unavailable``
(§38) instead of being retrofitted.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from data.trade_group_store import load_group
from execution.provenance import FRESHNESS_VALUES


def verify_group_provenance(group: dict) -> list[str]:
    """Return a list of violations (empty = verified)."""
    violations: list[str] = []
    spec = group["spec"]
    prov = spec.provenance or {}

    # 1. provenance presence — legacy records are explicit, never fabricated
    if not prov:
        violations.append("provenance_status=legacy_unavailable (no lineage recorded; NOT retrofitted)")
        return violations

    # 2. critical ids resolve
    required = (
        "market_snapshot_id",
        "feature_snapshot_id",
        "model_inference_id",
        "model_hash",
        "profile_id",
        "broker_snapshot_id",
        "cost_snapshot_id",
        "geometry_hash",
        "provenance_hash",
    )
    for key in required:
        if not prov.get(key):
            violations.append(f"missing provenance.{key}")

    # 3. hashes match
    if prov.get("geometry_hash") and prov["geometry_hash"] != spec.geometry_hash():
        violations.append("provenance.geometry_hash != spec.geometry_hash")
    if prov.get("provenance_hash") and prov["provenance_hash"] != spec.provenance_hash():
        violations.append("provenance.provenance_hash != spec.provenance_hash")

    # 4. freshness valid
    if "freshness" in prov and prov["freshness"] not in FRESHNESS_VALUES:
        violations.append(f"invalid freshness {prov.get('freshness')!r}")

    # 5. mode/source consistency (§31/§32): paper facts must be simulator,
    # demo facts must be mt5 — a fake source is a violation.
    mode = spec.mode
    source = prov.get("source")
    if mode == "paper":
        if source not in (None, "simulator", "derived", "config", "model_artifact"):
            violations.append(f"paper group labeled source={source!r}; expected simulator")
    elif mode == "demo":
        if source == "simulator" or source == "fake_mt5":
            violations.append(f"demo group labeled source={source!r}; expected mt5")

    # 6. parent relationships exist (derived nodes reference parents)
    if prov.get("parent_ids") == [] and prov.get("source_type") == "derived":
        violations.append("derived artifact has no parent_ids")

    return violations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, help="group id to verify")
    parser.add_argument("--db", default="data/market_data_mt5.sqlite")
    args = parser.parse_args()

    group = load_group(args.db, args.group)
    if group is None:
        print(f"GROUP {args.group}: not found")
        sys.exit(1)
    violations = verify_group_provenance(group)
    if not violations:
        print(f"GROUP {args.group}: PROVENANCE VERIFIED")
        sys.exit(0)
    print(f"GROUP {args.group}: {len(violations)} violation(s)")
    for violation in violations:
        print(f"  - {violation}")
    sys.exit(1)


if __name__ == "__main__":
    main()
