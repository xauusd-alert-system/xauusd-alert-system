#!/usr/bin/env python3
"""Manual update: copy config/us_stocks_challenge.yaml holidays to data/us_market_holidays.json

Usage:
    python scripts/update_holidays.py
    python scripts/update_holidays.py --check  # fail if mismatch
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def load_config_holidays(config_path: Path) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sess = (cfg or {}).get("session", {})
    return {
        "holidays": sess.get("holidays", []),
        "early_closes": sess.get("early_closes", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Update/sync holidays JSON")
    parser.add_argument("--check", action="store_true", help="Check sync without writing")
    parser.add_argument("--config", default="config/us_stocks_challenge.yaml")
    parser.add_argument("--output", default="data/us_market_holidays.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    output_path = Path(args.output)

    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        return 1

    cfg_data = load_config_holidays(config_path)

    if args.check:
        if not output_path.exists():
            print(f"Missing {output_path} — run without --check to create", file=sys.stderr)
            return 1
        with open(output_path, encoding="utf-8") as f:
            existing = json.load(f)
        if existing != cfg_data:
            print("Holidays mismatch between config and JSON", file=sys.stderr)
            print(f"Config: {cfg_data}")
            print(f"JSON:   {existing}")
            return 1
        print("Holidays in sync")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cfg_data, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Updated {output_path}")
    print(f"  holidays: {len(cfg_data['holidays'])} dates")
    print(f"  early_closes: {len(cfg_data['early_closes'])} dates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
