"""Pre-lock controlled MTF contribution test for XAUUSD.

Compares the current duplicated-base reference [M15,H1] with [H1,H4].  It
refuses to cross the configured locked hold-out and writes explicit source/mode/
as-of metadata. Nothing is deployed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import UTC, datetime

import pandas as pd

from config.loader import load_config
from scripts.deflated_sharpe import run_analysis
from scripts.run_backtest import build_full_df, load_asset_history, truncate_before


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", default="XAUUSD")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output", default="logs/mtf_reference_comparison.json")
    parser.add_argument("--max-folds", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    lock = cfg.get("validation", {}).get("locked_holdout", {})
    lock_start = lock.get("start") if lock.get("enabled") else None
    if lock_start and pd.Timestamp(args.end_date, tz="UTC") > pd.Timestamp(lock_start, tz="UTC"):
        raise SystemExit(f"end-date {args.end_date} crosses locked hold-out start {lock_start}; refusing")
    asset_cfg = cfg.get("assets", {}).get(args.asset)
    if not asset_cfg:
        raise SystemExit(f"unknown asset {args.asset}")
    timeframe = asset_cfg.get("timeframe", "M15")
    raw = truncate_before(load_asset_history(args.db_path, timeframe, args.asset), args.end_date, args.asset)

    candidates = {
        "current_M15_H1": ["M15", "H1"],
        "candidate_H1_H4": ["H1", "H4"],
    }
    results = {}
    for name, refs in candidates.items():
        cfg_i = copy.deepcopy(cfg)
        cfg_i.setdefault("features", {})["mtf_reference_timeframes"] = refs
        featured = build_full_df(cfg_i, raw, args.db_path, args.asset)
        analysis = run_analysis(
            cfg_i,
            args.asset,
            featured,
            variants={"current": {}},
            historical_trials=737,
            max_folds=args.max_folds,
            cost_stress=True,
        )
        results[name] = {
            "references": refs,
            "effective_config_sha256": _hash(cfg_i),
            "trial": analysis["trials"][0],
        }

    report = {
        "asset_key": args.asset,
        "base_timeframe": timeframe,
        "sample_end_exclusive_utc": args.end_date,
        "locked_holdout_read": False,
        "source": "copied_real_sqlite_prelock",
        "mode": "research_only_not_deployed",
        "as_of_utc": datetime.now(UTC).isoformat(),
        "results": results,
    }
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
