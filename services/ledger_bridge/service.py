"""Ledger Bridge service (TZ 8.1) — standalone delivery process.

Thin wrapper around ``data/ledger_bridge.py`` (outbox + signed delivery) and
``scripts/run_ledger_bridge.py`` (CLI semantics). No delivery logic is
duplicated: the service runs the existing ``run_delivery_loop`` / ``--once``
path in its own process and serves a health endpoint with two checks:

* ``ingest_db``        — the outbox database is readable and the table exists;
* ``outbox_watermark`` — P2-19: with pending events, the delivery watermark
  (max ``delivered_at_ms``) must have moved within ``watermark_max_age_min``;
  with an empty outbox there is nothing to move, so the check passes.

Run: ``python -m services.ledger_bridge [--once] [--health-port 8791]``
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.base import start_health_server_thread  # noqa: E402

DEFAULT_HEALTH_PORT = 8791
DEFAULT_WATERMARK_MAX_AGE_MIN = 30.0

SERVICE_NAME = "ledger_bridge"


def _latest_delivered_at_ms(db_path: str) -> int | None:
    import sqlite3

    from data.storage import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(delivered_at_ms) FROM ledger_outbox"
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def make_ingest_db_check(db_path: str) -> Callable[[], tuple[bool, str]]:
    """Health check: outbox DB opens and the ``ledger_outbox`` table exists."""

    def check() -> tuple[bool, str]:
        if not os.path.exists(db_path):
            return False, f"outbox db not found: {db_path}"
        import sqlite3

        from data.storage import get_connection

        try:
            conn = get_connection(db_path)
        except Exception as exc:
            return False, f"outbox db unavailable: {exc}"
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ledger_outbox'"
            ).fetchone()
            if row is None:
                return False, "table ledger_outbox missing (run init_outbox)"
        except sqlite3.OperationalError as exc:
            return False, f"outbox db query failed: {exc}"
        finally:
            conn.close()
        return True, "ok"

    return check


def make_watermark_check(
    db_path: str, max_age_minutes: float = DEFAULT_WATERMARK_MAX_AGE_MIN
) -> Callable[[], tuple[bool, str]]:
    """P2-19 health check: delivery watermark is moving (or outbox is idle).

    * no pending rows -> ok (nothing to deliver);
    * pending rows and the latest delivered_at_ms is within the age budget -> ok;
    * pending rows and no delivery has EVER happened (or the watermark is
      older than the budget) -> degraded.
    """
    from data.ledger_bridge import outbox_stats

    def check() -> tuple[bool, str]:
        try:
            stats = outbox_stats(db_path)
        except Exception as exc:
            return False, f"outbox stats unavailable: {exc}"
        pending = int(stats.get("pending") or 0)
        if pending == 0:
            return True, f"ok (pending=0, delivered={stats.get('delivered')})"
        latest = _latest_delivered_at_ms(db_path)
        if latest is None:
            return (
                False,
                f"degraded: {pending} pending events and the delivery watermark "
                f"has never moved",
            )
        age_minutes = (time.time_ns() // 1_000_000 - latest) / 60_000.0
        if age_minutes > float(max_age_minutes):
            return (
                False,
                f"degraded: watermark stale {age_minutes:.1f} min "
                f"(budget {float(max_age_minutes):.1f} min, pending={pending})",
            )
        return True, f"ok (watermark age {age_minutes:.1f} min, pending={pending})"

    return check


def build_checks(db_path: str, watermark_max_age_min: float = DEFAULT_WATERMARK_MAX_AGE_MIN) -> dict:
    """Assemble the service checks dict (unit-tested without the network)."""
    return {
        "ingest_db": make_ingest_db_check(db_path),
        "outbox_watermark": make_watermark_check(db_path, watermark_max_age_min),
    }


def _bridge_config():
    """Reuse the existing strict config resolution (never duplicated here)."""
    from config.loader import load_config
    from data.ledger_bridge import load_bridge_config

    return load_bridge_config(load_config())


def run(args: argparse.Namespace) -> None:
    """Entry point: start health server, then run the existing delivery loop."""
    checks = build_checks(args.db_path, args.watermark_max_age_min)
    server = start_health_server_thread(args.health_port, checks)

    if args.once:
        from data.ledger_bridge import deliver_outbox, outbox_stats

        bridge = _bridge_config()
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
    else:
        from data.ledger_bridge import run_delivery_loop

        bridge = _bridge_config()
        interval = args.interval if args.interval is not None else bridge["interval_seconds"]

        def _report(result: dict) -> None:
            print(f"[{os.getpid()}] {result}")

        try:
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
        finally:
            server.should_exit = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {__name__.rsplit('.', 1)[0]}",
        description="Ledger Bridge service (TZ 8.1): signed outbox delivery "
        "with a health endpoint (P2-19 watermark check).",
    )
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--account-mode", default="demo",
                        choices=["demo", "contest", "real"])
    parser.add_argument("--account-login", default="0")
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--once", action="store_true",
                        help="deliver one batch and exit")
    parser.add_argument("--health-port", type=int, default=DEFAULT_HEALTH_PORT)
    parser.add_argument("--watermark-max-age-min", type=float,
                        default=DEFAULT_WATERMARK_MAX_AGE_MIN)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
