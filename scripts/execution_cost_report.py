"""Print empirical broker fill/slippage/latency distributions as JSON."""
import argparse
import json

from data.execution_ledger import broker_spread_report, execution_cost_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--timeframe", default=None, help="Also report raw broker bar spread")
    parser.add_argument("--output", default=None, help="Optional JSON report path")
    args = parser.parse_args()
    report = {"execution_fills": execution_cost_report(args.db_path, args.asset)}
    if args.timeframe and args.asset:
        report["broker_bar_spread"] = broker_spread_report(
            args.db_path, args.timeframe, args.asset
        )
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")


if __name__ == "__main__":
    main()
