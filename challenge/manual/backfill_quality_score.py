# -*- coding: utf-8 -*-
"""Backfill quality_score for existing alerts in alerts_sent.json.

Reads each alert, computes quality_score from available data, and updates
the record. Run once to fix historical alerts that were created before
quality_score tracking was added.
"""
from __future__ import annotations

import json
import os
import sys
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SENT_FILE = os.path.join(ROOT, "data", "manual", "alerts_sent.json")


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def backfill():
    """Add quality_score=0 and quality_components={} to all alerts that don't have them."""
    sent = _load_json(SENT_FILE)
    changed = 0
    for key, rec in sent.items():
        if not isinstance(rec, dict):
            continue
        if "quality_score" not in rec:
            rec["quality_score"] = 0
            changed += 1
        if "quality_components" not in rec:
            rec["quality_components"] = {}
            changed += 1
        print(f"  Backfilled: {key} -> quality_score={rec.get('quality_score', 0)}")
    
    if changed > 0:
        _save_json(SENT_FILE, sent)
        print(f"\nBackfilled {changed} alerts")
    else:
        print("All alerts already have quality_score and quality_components")
    
    return changed


def main() -> int:
    print("=== Backfill quality_score ===")
    n = backfill()
    print(f"\nDone. {n} records updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
