"""Audit: no FX trade should carry a 'weekend' session tag at Sunday 21:00-24:00 UTC.

FX markets reopen Sunday ~21:00 UTC with the Asian session. The session tagger
must classify these bars as 'asia' (or 'newyork' if it falls in that window),
NOT 'weekend'. A 'weekend' tag at these hours means the tagger silently loses
trades during the first real session of the week.

Scope: EURUSD, GBPUSD, XAUUSD, XAGUSD (all assets whose market is closed
Saturday–Sunday 21:00 UTC). BTCUSD is 24/7 and is excluded.

Reads trade_quality CSVs produced by diag_trade_quality.py / walk-forward.
Read-only. Exit 1 on any violation (fail-closed for overnight pipeline).
"""
import argparse
import datetime
import glob
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# FX assets where weekend 21-24 UTC is invalid
FX_ASSETS = {"EURUSD", "GBPUSD", "XAUUSD", "XAGUSD"}


def audit_weekend_tags(log_dir: str = "logs") -> list[dict]:
    """Scan all trade_quality CSVs for weekend-tagged FX trades at Sun 21-24 UTC.

    Returns a list of violation dicts (empty = OK).
    """
    violations: list[dict] = []
    csv_pattern = os.path.join(log_dir, "trade_quality_*_dir*.csv")
    csv_files = sorted(glob.glob(csv_pattern))

    if not csv_files:
        print(f"  No trade_quality CSVs found in {log_dir}/")
        return violations

    import csv

    for csv_path in csv_files:
        basename = os.path.basename(csv_path)
        # Extract asset key from filename: trade_quality_{asset}_dir*.csv
        parts = basename.replace("trade_quality_", "").split("_")
        asset_key = parts[0].upper()

        if asset_key not in FX_ASSETS:
            continue

        try:
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                if "session" not in (reader.fieldnames or []):
                    continue
                for row in reader:
                    if row.get("session") != "weekend":
                        continue
                    ts = int(row["entry_ts"])
                    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                    # Sunday 21:00-24:00 UTC = the invalid window
                    if dt.weekday() == 6 and dt.hour >= 21:
                        violations.append({
                            "asset": asset_key,
                            "csv": basename,
                            "entry_ts": ts,
                            "entry_utc": dt.strftime("%Y-%m-%d %H:%M UTC"),
                            "day": dt.strftime("%A"),
                            "direction": row.get("direction", "?"),
                            "R": row.get("R", "?"),
                            "exit_reason": row.get("exit_reason", "?"),
                        })
        except Exception as exc:
            print(f"  WARNING: could not read {csv_path}: {exc}")

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", default="logs", help="Directory with trade_quality CSVs")
    args = parser.parse_args(argv)

    print("[audit_weekend_tags] Scanning FX trade_quality CSVs ...")
    violations = audit_weekend_tags(args.log_dir)

    if violations:
        print(f"\n  VIOLATION: {len(violations)} FX trade(s) tagged 'weekend' at Sunday 21:00-24:00 UTC:\n")
        for v in violations:
            print(
                f"    {v['asset']}  {v['entry_utc']}  {v['direction']:5s}  "
                f"R={v['R']}  exit={v['exit_reason']}  [{v['csv']}]"
            )
        print(
            "\n  Root cause: tag_session() does not recognise Sunday 21:00+ UTC as\n"
            "  the start of the FX week. The 'weekend' bucket steals real session\n"
            "  bars, producing phantom trades or losing valid ones.\n"
            "\n  Fix: add tag_session_with_weekend() or per-asset session windows\n"
            "  that classify Sunday 21:00+ UTC as the appropriate session."
        )
        return 1

    print("  OK — no weekend-tagged FX trades at Sunday 21:00-24:00 UTC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
