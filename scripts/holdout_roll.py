"""
Rolling hold-out gate (Rolling Holdout policy, Step 2 - 2026-08-30).

When the nightly retrain is frozen on data up to validation.locked_holdout.start
(see scripts/overnight.py post-fix), the production model silently ages on the
live-forward accumulation. This module makes the lock shift a *conscious,
preregistered, journalled* act instead of a silent one:

    propose(old_start, new_start)
        Train a CANDIDATE only on data strictly before `new_start`. Score BOTH
        the candidate and the incumbent (current production model) ONLY on the
        released slice [old_start, new_start). Decide via deploy_guard.is_improvement.

    journal (logs/holdout_roll_journal.csv, append-only)
        Every propose / move / rollback is recorded: ts, old_start, new_start,
        gate_metrics_json, decision, actor, rollback_of.

    CI guard (scripts/check_holdout_roll.py)
        config.validation.locked_holdout.start MUST equal the journal's
        effective current start (or the preregistered baseline). Catches a lock
        moved out-of-band: edited in config but not journalled, or journalled
        but never applied.

NO config editing happens here. Shifting the lock to new_start is the OWNER's
act, performed in a REAL terminal (never from the sandbox). This script only
proposes, records, and verifies.

Preregistered constants (cadence, step, tolerance, min_trades) live in
config.yaml under `holdout_roll:` - there are no bare magic numbers in the gate
logic. The no-look-ahead contract is enforced in two places:

  * R4 (training) - the candidate frame is truncated to strictly-before
    `new_start` BEFORE feature building, so no fold is ever fit on data at/after
    the new lock.
  * R4 (scoring) - only windows fully inside [old_start, new_start) are scored
    (see released_windows); nothing outside the released slice is touched.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))

from config.loader import load_config
from data.storage import read_candles
from scripts.deploy_guard import (
    enabled_assets,
    evaluate_candidate,
    evaluate_incumbent,
    is_improvement,
)
from scripts.train_mt5 import build_full_df, truncate_raw_before

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("holdout_roll")

JOURNAL_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "logs", "holdout_roll_journal.csv")
)
JOURNAL_COLUMNS = [
    "ts",
    "old_start",
    "new_start",
    "gate_metrics_json",
    "decision",
    "actor",
    "rollback_of",
]


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def _hr(cfg: dict) -> dict:
    return cfg.get("holdout_roll", {})


def _db_path(cfg: dict) -> str:
    return cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")


def _timeframe(cfg: dict, asset_key: str) -> str:
    return (
        cfg.get("assets", {}).get(asset_key, {}).get("timeframe")
        or cfg.get("market_data", {}).get("timeframe", "M15")
    )


def _wf_days(cfg: dict) -> tuple[int, int, int]:
    wf = cfg.get("backtest", {}).get("walk_forward", {})
    return (
        int(wf.get("train_window_days", 300)),
        int(wf.get("test_window_days", 50)),
        int(wf.get("step_days", 50)),
    )


def _to_ts(date_str: str) -> int:
    """Parse a YYYY-MM-DD (UTC) date to an epoch-second integer."""
    return int(pd.Timestamp(date_str, tz="UTC").timestamp())


def current_lock_start(cfg: dict) -> str | None:
    """The config's current validation.locked_holdout.start (or None)."""
    lock = cfg.get("validation", {}).get("locked_holdout", {}) or {}
    return str(lock["start"]) if lock.get("start") else None


# --------------------------------------------------------------------------- #
# Pure slice filter (R4 - scoring only inside the released window)
# --------------------------------------------------------------------------- #
def released_windows(windows, old_start_ts: int, new_start_ts: int) -> list:
    """Return only the windows whose OOS test span lies inside [old_start, new_start).

    Pure and trivially unit-testable. Guarantees the gate never scores data
    outside the released slice (R4 part 2).
    """
    out = []
    for w in windows:
        if w.test_start_ts >= old_start_ts and w.test_end_ts <= new_start_ts:
            out.append(w)
    return out


# --------------------------------------------------------------------------- #
# Journal (append-only)
# --------------------------------------------------------------------------- #
def append_journal_row(row: dict) -> None:
    """Append one row to logs/holdout_roll_journal.csv (header written once)."""
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    write_header = not os.path.exists(JOURNAL_PATH) or os.path.getsize(JOURNAL_PATH) == 0
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_COLUMNS)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in JOURNAL_COLUMNS})


def read_journal() -> list[dict]:
    """Return all journal rows as dicts (oldest first). Empty list if no file."""
    if not os.path.exists(JOURNAL_PATH):
        return []
    with open(JOURNAL_PATH, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def last_row() -> dict | None:
    rows = read_journal()
    return rows[-1] if rows else None


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def propose(
    old_start: str,
    new_start: str,
    cfg: dict | None = None,
    asset_key: str | None = None,
    actor: str = "sandbox-propose",
) -> dict:
    """Isolated released-window gate.

    * candidate is trained ONLY on data strictly before `new_start` (R4, training);
    * both the candidate and the incumbent (current production model) are scored
      ONLY on [old_start, new_start) (R4, scoring);
    * the candidate-vs-incumbent decision is made via deploy_guard.is_improvement
      using the preregistered holdout_roll.* constants.

    Returns a result dict and appends a PROPOSED journal row. Does NOT edit config.

    The gate passes only if EVERY evaluated asset passes is_improvement.
    """
    if cfg is None:
        cfg = load_config()
    hr = _hr(cfg)
    primary = str(hr.get("primary_metric", "expectancy"))
    tolerance = float(hr.get("tolerance", 0.0))
    min_trades = int(hr.get("released_min_trades", 20))
    fallback = tuple(hr.get("fallback_metrics", ["sharpe_ratio", "win_rate", "total_pnl"]))

    old_ts = _to_ts(old_start)
    new_ts = _to_ts(new_start)
    if new_ts <= old_ts:
        raise ValueError(f"new_start ({new_start}) must be strictly after old_start ({old_start})")

    assets = [asset_key] if asset_key else enabled_assets(cfg)
    if not assets:
        raise ValueError("no enabled assets to evaluate the gate against")

    per_asset: dict[str, dict] = {}
    for asset in assets:
        raw = read_candles(_db_path(cfg), _timeframe(cfg, asset), asset)
        if raw.empty:
            per_asset[asset] = {"asset": asset, "deploy": False, "reason": "no_candles"}
            continue

        # R4 part 1: candidate training never sees data at/after new_start.
        cand_raw = truncate_raw_before(raw, new_start, asset)
        cand_df = build_full_df(
            cand_raw, cfg, db_path=_db_path(cfg), asset_key=asset, timeframe=_timeframe(cfg, asset)
        )
        cand_df["timestamp_utc"] = cand_df["timestamp_utc"].astype("int64")

        windows = generate_windows_safe(cand_df, *_wf_days(cfg))
        rel = released_windows(windows, old_ts, new_ts)
        if not rel:
            per_asset[asset] = {
                "asset": asset,
                "deploy": False,
                "reason": "no_released_windows",
                "n_windows": len(windows),
            }
            continue

        dep = cfg.get("assets", {}).get(asset, {}).get("model_path")
        inc_m = evaluate_incumbent(cfg, asset, dep, cand_df, rel) if dep and os.path.exists(dep) else None
        can_m = evaluate_candidate(cfg, asset, cand_df, rel)
        dec = is_improvement(
            inc_m or {},
            can_m or {},
            primary=primary,
            tolerance=tolerance,
            min_trades=min_trades,
            fallback_chain=fallback,
        )
        dec["asset"] = asset
        dec["released_folds"] = len(rel)
        per_asset[asset] = dec

    gate_pass = bool(per_asset) and all(bool(d.get("deploy", False)) for d in per_asset.values())
    result = {
        "old_start": old_start,
        "new_start": new_start,
        "primary_metric": primary,
        "tolerance": tolerance,
        "released_min_trades": min_trades,
        "assets": per_asset,
        "gate_pass": gate_pass,
        "decision": "PROPOSED",
    }
    append_journal_row(
        {
            "old_start": old_start,
            "new_start": new_start,
            "gate_metrics_json": json.dumps(result, default=str),
            "decision": "PROPOSED",
            "actor": actor,
            "rollback_of": "",
        }
    )
    return result


def generate_windows_safe(df: pd.DataFrame, train_days: int, test_days: int, step_days: int) -> list:
    """Thin wrapper so propose does not import generate_windows at module top
    twice; centralises the walk-forward call. Returns [] on empty frames."""
    # Imported lazily to keep the module import cheap for the CI guard path.
    from backtest.walk_forward import generate_windows

    if df is None or len(df) == 0:
        return []
    return generate_windows(df, train_days, test_days, step_days)


# --------------------------------------------------------------------------- #
# Human-in-the-loop: owner moves / rolls back (record only - no config edit)
# --------------------------------------------------------------------------- #
def move_lock(old_start: str, new_start: str, actor: str = "owner") -> dict:
    """Record the owner's decision to shift the lock to new_start.

    Does NOT edit config - shifting the lock is the owner's act in a real
    terminal (see docs/HOLDOUT_ROLL_POLICY.md runbook). The CI guard treats
    this MOVED row as the system of record for the intended lock start.

    The appended journal row is the audit trail and MUST be committed together
    with the config shift: `logs/` is gitignored (`logs/*.csv`), so the owner
    must `git add -f logs/holdout_roll_journal.csv` in the same commit, or the
    CI guard will not see the journal after checkout and falsely fail.
    """
    append_journal_row(
        {
            "old_start": old_start,
            "new_start": new_start,
            "gate_metrics_json": "",
            "decision": "MOVED",
            "actor": actor,
            "rollback_of": "",
        }
    )
    return {"decision": "MOVED", "old_start": old_start, "new_start": new_start}


def rollback(rolled_back_new_start: str, revert_to: str | None = None, actor: str = "owner") -> dict:
    """Record a rollback of a previously moved lock.

    `rolled_back_new_start` is the start that was moved and is now being undone.
    `revert_to` is the start config should revert to (the predecessor). If not
    given, it is looked up from the MOVED journal row being rolled back. Does
    NOT edit config - the owner reverts config.validation.locked_holdout.start
    in a real terminal.

    Journal row: old_start = the value we leave (rolled_back_new_start),
    new_start = the value config reverts to, rollback_of = rolled_back_new_start.

    As with move_lock, the appended journal row is the audit trail and MUST be
    committed with `git add -f logs/holdout_roll_journal.csv` (logs/ is
    gitignored) in the same commit as the config revert, so the CI guard reads
    it after checkout.
    """
    if revert_to is None:
        for row in read_journal():
            if row["decision"] == "MOVED" and row["new_start"] == rolled_back_new_start:
                revert_to = row["old_start"]
                break
    if revert_to is None:
        raise ValueError(
            f"cannot resolve predecessor for rollback of {rolled_back_new_start}; "
            "pass --revert-to explicitly"
        )
    append_journal_row(
        {
            "old_start": rolled_back_new_start,
            "new_start": revert_to,
            "gate_metrics_json": "",
            "decision": "ROLLED_BACK",
            "actor": actor,
            "rollback_of": rolled_back_new_start,
        }
    )
    return {
        "decision": "ROLLED_BACK",
        "old_start": rolled_back_new_start,
        "revert_to": revert_to,
        "rollback_of": rolled_back_new_start,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _actor() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rolling hold-out lock gate (preregistered policy).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="Run the released-window gate for a candidate lock shift.")
    p.add_argument("--old-start", required=True, help="Current lock start (YYYY-MM-DD).")
    p.add_argument("--new-start", required=True, help="Proposed new lock start (YYYY-MM-DD).")
    p.add_argument("--asset", default=None, help="Single asset to evaluate (default: all enabled).")
    p.add_argument("--actor", default=None, help="Who is proposing (default: current user).")

    m = sub.add_parser("move", help="Record the owner's decision to shift the lock (no config edit).")
    m.add_argument("--old-start", required=True, help="Current lock start (YYYY-MM-DD).")
    m.add_argument("--new-start", required=True, help="New lock start being applied (YYYY-MM-DD).")
    m.add_argument("--actor", default=None, help="Who is moving (default: current user).")

    r = sub.add_parser("rollback", help="Record a rollback of a previously moved lock (no config edit).")
    r.add_argument("--new-start", required=True, help="The start being rolled back (YYYY-MM-DD).")
    r.add_argument("--revert-to", default=None, help="Start config reverts to (default: lookup).")
    r.add_argument("--actor", default=None, help="Who is rolling back (default: current user).")

    j = sub.add_parser("journal", help="Print the journal.")
    j.add_argument("--last", action="store_true", help="Print only the last row.")

    args = parser.parse_args(argv)

    if args.cmd == "propose":
        res = propose(
            args.old_start,
            args.new_start,
            asset_key=args.asset,
            actor=args.actor or _actor(),
        )
        print(json.dumps(res, indent=2, default=str))
        # The gate verdict is informational; the owner still has to OK + move.
        return 0 if res["gate_pass"] else 0

    if args.cmd == "move":
        out = move_lock(args.old_start, args.new_start, actor=args.actor or _actor())
        print(json.dumps(out, indent=2))
        print(
            "REMINDER: shift the lock in a REAL terminal - edit "
            "config.validation.locked_holdout.start and commit. Not done here."
        )
        return 0

    if args.cmd == "rollback":
        out = rollback(args.new_start, revert_to=args.revert_to, actor=args.actor or _actor())
        print(json.dumps(out, indent=2))
        print(
            "REMINDER: revert the lock in a REAL terminal - edit "
            "config.validation.locked_holdout.start back to "
            f"{out['revert_to']} and commit. Not done here."
        )
        return 0

    if args.cmd == "journal":
        rows = read_journal()
        if args.last:
            print(json.dumps(last_row(), indent=2) if rows else "{}")
        else:
            for row in rows:
                print(json.dumps(row))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
