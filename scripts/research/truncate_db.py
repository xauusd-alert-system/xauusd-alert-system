"""Truncate ohlcv_* tables in a research SQLite DB newer than a cutoff date.

P2-30 (TZ Часть 7): this script deletes data — it now requires an explicit
``--dry-run`` to preview or ``--yes`` to confirm an interactive-style
deletion. Non-interactive runs (CI, automation) must pass ``--yes``.

Usage:
    python scripts/research/truncate_db.py --dry-run                # preview
    python scripts/research/truncate_db.py --yes                    # interactive confirm
    python scripts/research/truncate_db.py --yes --no-confirm       # non-interactive delete
    python scripts/research/truncate_db.py --db PATH --cutoff 2026-08-08 --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete rows with timestamp_utc >= cutoff from ohlcv_* tables."
    )
    parser.add_argument(
        "--db",
        default="data/research_prelock.sqlite",
        help="Path to the SQLite database (default: data/research_prelock.sqlite)",
    )
    parser.add_argument(
        "--cutoff",
        default="2026-08-08",
        help="UTC date (YYYY-MM-DD); rows with timestamp_utc >= its epoch are deleted "
        "(default: 2026-08-08)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many rows would be deleted per table without touching the DB",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly acknowledge deletion; non-interactive runs REQUIRE "
        "--yes together with --no-confirm",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip the [y/N] prompt (requires --yes; intended for automation)",
    )
    return parser


def cutoff_epoch(cutoff: str) -> int:
    dt = datetime.datetime.strptime(cutoff, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp())


def ohlcv_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "select name from sqlite_master "
        "where type='table' and name like 'ohlcv_%'"
    ).fetchall()
    return [r[0] for r in rows]


def count_rows_to_delete(con: sqlite3.Connection, table: str, cut: int) -> int:
    return int(
        con.execute(
            f"select count(*) from \"{table}\" where timestamp_utc >= ?", (cut,)
        ).fetchone()[0]
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cut = cutoff_epoch(args.cutoff)

    con = sqlite3.connect(args.db)
    try:
        tables = ohlcv_tables(con)

        if args.dry_run:
            total = 0
            print(f"DRY RUN — no rows will be deleted (db={args.db}, cutoff={args.cutoff})")
            for t in tables:
                n = count_rows_to_delete(con, t, cut)
                total += n
                print(f"  would delete {n:8d} rows from {t}")
            print(f"TOTAL rows that would be deleted: {total}")
            return 0

        if not args.yes:
            print(
                "Refusing to delete without explicit consent.\n"
                "Use --dry-run to preview, or --yes (with --no-confirm for "
                "non-interactive runs)."
            )
            return 2

        if not args.no_confirm:
            print(
                f"Delete rows with timestamp_utc >= {args.cutoff} from "
                f"{len(tables)} ohlcv_* tables in {args.db}?"
            )
            answer = input("Are you sure? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted — nothing deleted.")
                return 1

        deleted = []
        for t in tables:
            n = count_rows_to_delete(con, t, cut)
            con.execute(f"delete from \"{t}\" where timestamp_utc >= ?", (cut,))
            deleted.append((t, n))
        con.commit()
        print("truncated:", [t for t, _ in deleted])
        for t, n in deleted:
            print(f"  deleted {n:8d} rows from {t}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
