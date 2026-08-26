# -*- coding: utf-8 -*-
"""Backfill setup_type in outcomes_resolved.json from alerts_sent.json.

Some resolved records were created before setup_type tracking was added.
This script reads alerts_sent.json to get the setup_type and updates
outcomes_resolved.json accordingly.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SENT_FILE = os.path.join(ROOT, "data", "manual", "alerts_sent.json")
RESOLVED_FILE = os.path.join(ROOT, "data", "manual", "outcomes_resolved.json")


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def backfill():
    """Add setup_type to resolved records that don't have it."""
    sent = _load_json(SENT_FILE)
    resolved = _load_json(RESOLVED_FILE)
    changed = 0
    
    for key, rec in resolved.items():
        if not isinstance(rec, dict):
            continue
        if "setup_type" not in rec and key in sent:
            alert = sent[key]
            if isinstance(alert, dict):
                setup_type = alert.get("setup_type", "")
                if setup_type:
                    rec["setup_type"] = setup_type
                    changed += 1
                    print(f"  Backfilled: {key} -> setup_type={setup_type}")
    
    if changed > 0:
        _save_json(RESOLVED_FILE, resolved)
        print(f"\nBackfilled {changed} resolved records with setup_type")
    else:
        print("All resolved records already have setup_type")
    
    return changed


def main() -> int:
    print("=== Backfill setup_type in outcomes ===")
    n = backfill()
    print(f"\nDone. {n} records updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
