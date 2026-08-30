"""Build an immutable data-provenance manifest for one asset/timeframe export.

Example:
    python -m scripts.build_provenance_manifest --asset XAUUSD --timeframe M15 \
        --db-path data/market_data_mt5.sqlite \
        --broker FxPro --broker-symbol GOLD \
        --out config/provenance/xauusd_m15_fxpro.json

After the manifest exists, set in config.yaml:

    validation:
      require_provenance_manifest: true
      provenance_manifest_path: config/provenance/xauusd_m15_fxpro.json

so ``train_mt5`` / ``run_backtest`` verify the frozen content fingerprint
before every run and stop on any mismatch.
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

from config.loader import load_config
from data.provenance import build_provenance_manifest, write_provenance_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", required=True, help="Internal asset key, e.g. XAUUSD")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--broker", required=True, help="Broker name as shown by the terminal")
    parser.add_argument("--broker-symbol", default=None, help="Broker-side symbol, e.g. GOLD")
    parser.add_argument("--company-hash", default=None, help="sha256 of terminal company string")
    parser.add_argument("--terminal-build", default=None)
    parser.add_argument("--terminal-path-hash", default=None)
    parser.add_argument(
        "--server-offset-hours",
        type=float,
        default=None,
        help="Broker server timezone offset vs UTC (config market_data.server_time_offset_hours)",
    )
    parser.add_argument("--out", required=True, help="Output manifest path (json)")
    args = parser.parse_args()

    cfg = load_config()
    offset = args.server_offset_hours
    if offset is None:
        offset = float((cfg.get("market_data", {}) or {}).get("server_time_offset_hours", 0.0))
    sessions = cfg.get("sessions", {}) or {}
    manifest = build_provenance_manifest(
        args.db_path,
        args.timeframe,
        args.asset,
        broker=args.broker,
        broker_symbol=args.broker_symbol,
        terminal_company_hash=args.company_hash,
        terminal_build=args.terminal_build,
        terminal_data_path_hash=args.terminal_path_hash,
        server_time_offset_hours=offset,
        sessions_config=sessions,
        extra={
            "command": "python -m scripts.build_provenance_manifest " + " ".join(sys.argv[1:]),
            "broker_symbol_input": args.broker_symbol,
        },
    )
    write_provenance_manifest(args.out, manifest)
    print(
        json.dumps(
            {
                "manifest_path": args.out,
                "asset_key": args.asset,
                "timeframe": args.timeframe,
                "candle_count": manifest["candle_count"],
                "data_hash": manifest["data_hash"],
                "manifest_hash": manifest["manifest_hash"],
                "gap_audit": manifest["gap_audit"],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
