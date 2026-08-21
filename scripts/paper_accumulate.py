"""Create and run a frozen, append-only live-forward paper accumulator."""
from __future__ import annotations

import argparse
import json
import time

import pandas as pd

from config.loader import load_config
from data.paper_ledger import paper_accumulation_status
from paper.accumulator import (
    FrozenPaperAccumulator,
    create_frozen_manifest,
    format_accumulation_status,
    load_frozen_manifest,
)
from realtime.pipeline import RealtimePipeline
from scripts.run_scheduler import seconds_until_next_candle_close


def _timestamp(value: str) -> int:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.timestamp())


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-manifest")
    create.add_argument("--asset", required=True)
    create.add_argument("--variant", required=True)
    create.add_argument("--model-path", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("--start", required=True, help="UTC date/time, e.g. 2026-08-08")
    create.add_argument("--min-trades", type=int, default=50)

    run = sub.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--db-path", default="data/paper_forward.sqlite")
    run.add_argument("--once", action="store_true")
    run.add_argument("--n-candles", type=int, default=300)

    status = sub.add_parser("status")
    status.add_argument("--manifest", required=True)
    status.add_argument("--db-path", default="data/paper_forward.sqlite")
    status.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "create-manifest":
        manifest = create_frozen_manifest(
            load_config(), asset_key=args.asset, variant=args.variant,
            model_path=args.model_path, output_path=args.manifest,
            start_timestamp_utc=_timestamp(args.start),
            min_closed_trades=args.min_trades,
        )
        print(json.dumps({k: manifest[k] for k in (
            "run_id", "asset_key", "variant", "model_sha256", "manifest_sha256",
            "start_timestamp_utc", "min_closed_trades",
        )}, indent=2))
        return

    manifest = load_frozen_manifest(args.manifest, verify_model=args.command == "run")
    if args.command == "status":
        result = paper_accumulation_status(args.db_path, manifest["run_id"])
        print(json.dumps(result, indent=2) if args.json else format_accumulation_status(result))
        return

    accumulator = FrozenPaperAccumulator(manifest, args.db_path)
    pipeline = RealtimePipeline(
        cfg=manifest["config_snapshot"], model_path=manifest["model_path"],
        asset_key=manifest["asset_key"], data_mode="live",
    )
    while True:
        result = accumulator.process_once(pipeline, n_candles=args.n_candles)
        print(format_accumulation_status(result), flush=True)
        if args.once:
            return
        timeframe = manifest["config_snapshot"]["assets"][manifest["asset_key"]].get(
            "timeframe", "M15"
        )
        time.sleep(seconds_until_next_candle_close(timeframe))


if __name__ == "__main__":
    main()
