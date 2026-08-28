"""Run the ledger bridge delivery loop for one producer database.

Delivers pending ``ledger_outbox`` rows (written by the Python sender /
trader) to the Signal Desk ingest endpoint. Durable by design: events are
removed from the pending set only after an HTTP 2xx; the server dedupes by
deterministic event_id.

Example:
    LEDGER_INGEST_URL=https://host/api/ledger/ingest \
    LEDGER_INGEST_TOKEN=secret \
    python -m scripts.run_ledger_bridge --db-path data/market_data_mt5.sqlite \
        --account-mode demo --account-login 12345678 --once
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, ".")

from config.loader import load_config
from data.ledger_bridge import (
    deliver_outbox,
    load_bridge_config,
    outbox_stats,
    run_delivery_loop,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--account-mode", default="demo", choices=["demo", "contest", "real"])
    parser.add_argument("--account-login", default="0", help="MT5 login for the event fingerprint")
    parser.add_argument("--interval", type=float, default=None, help="Poll interval in seconds (default: config/env)")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--once", action="store_true", help="Deliver one batch (or drain if pending == 0) and exit")
    args = parser.parse_args()

    bridge = load_bridge_config(load_config())
    interval = args.interval if args.interval is not None else bridge["interval_seconds"]
    if args.once:
        result = deliver_outbox(
            args.db_path,
            ingest_url=bridge["ingest_url"],
            token=bridge["token"],
            secret=bridge["secret"],
            account_mode=args.account_mode,
            account_login=args.account_login,
            batch_size=args.batch_size,
        )
        print(f"batch: {result}")
        print(f"outbox: {outbox_stats(args.db_path)}")
        return

    def _report(result: dict) -> None:
        print(f"[{os.getpid()}] {result}")

    run_delivery_loop(
        args.db_path,
        ingest_url=bridge["ingest_url"],
        token=bridge["token"],
        secret=bridge["secret"],
        account_mode=args.account_mode,
        account_login=args.account_login,
        interval_seconds=interval,
        batch_size=args.batch_size,
        on_result=_report,
    )


if __name__ == "__main__":
    main()
