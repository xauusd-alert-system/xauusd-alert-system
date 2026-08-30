"""
Append-only trial journal (quant audit 2026-08-07, Claude plan, question 1,
organizational measure): every backtest/grid/assessment run writes one row to
`logs/trial_journal.csv` (date, experiment, asset, params, metrics). N_trials
for DSR is then taken from the journal's actual history, not from the last
grid — the project has already run more than 729 trials (conf/EV/divergence
filters, TF moves, BE variants, ...).

Also implements the LOCKED HOLD-OUT guard: a reserved period that research
must not look at (config `validation.locked_holdout`). Any walk-forward test
window overlapping the lock is rejected unless the runner is invoked with
--allow-locked.
"""

import csv
import json
import os
from datetime import UTC, datetime

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
JOURNAL_PATH = os.path.join(LOGS_DIR, "trial_journal.csv")
JOURNAL_COLUMNS = ["ts_utc", "experiment", "asset", "params_json", "metrics_json", "git_commit"]


def _git_commit() -> str:
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def log_trial(experiment: str, asset: str, params: dict, metrics: dict) -> None:
    """Append one immutable row to the journal."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    row = {
        "ts_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "experiment": experiment,
        "asset": asset,
        "params_json": json.dumps(params, sort_keys=True, default=str),
        "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
        "git_commit": _git_commit(),
    }
    new_file = not os.path.exists(JOURNAL_PATH)
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def read_journal() -> list[dict]:
    """All journal rows as dicts (empty list when the journal does not exist)."""
    if not os.path.exists(JOURNAL_PATH):
        return []
    with open(JOURNAL_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def count_trials(asset: str | None = None) -> int:
    """Number of recorded trials, optionally for one asset."""
    rows = read_journal()
    if asset:
        rows = [r for r in rows if r.get("asset") == asset]
    return len(rows)


def default_historical_trials(asset: str, floor: int = 729) -> int:
    """DSR deflation N: the journal's real trial count for the asset (>= 2),
    else the project-history floor (729 = the original grid)."""
    n = count_trials(asset)
    return max(n, floor)


# ---------------------------------------------------------------------------
# Locked hold-out guard
# ---------------------------------------------------------------------------


def locked_holdout_config(cfg: dict) -> dict:
    return cfg.get("validation", {}).get("locked_holdout", {}) or {}


def locked_holdout_violations(cfg: dict, windows) -> list:
    """Test windows overlapping the locked period -> [(window, start_utc, end_utc)].
    `windows` is the walk-forward window list (dataclasses with test_start_ts /
    test_end_ts in epoch seconds). Returns [] when the lock is disabled."""
    lh = locked_holdout_config(cfg)
    if not lh.get("enabled", False):
        return []
    start = lh.get("start")
    end = lh.get("end")
    if not start and not end:
        return []
    start_s = int(pd_timestamp_epoch(start)) if start else None
    end_s = int(pd_timestamp_epoch(end)) if end else None
    bad = []
    for w in windows:
        w_start, w_end = int(w.test_start_ts), int(w.test_end_ts)
        overlap = True
        if start_s is not None and w_end <= start_s:
            overlap = False
        if end_s is not None and w_start >= end_s:
            overlap = False
        if overlap:
            bad.append({"window": w, "test_start_utc": _fmt_utc(w_start), "test_end_utc": _fmt_utc(w_end)})
    return bad


def pd_timestamp_epoch(iso_or_date: str) -> float:
    import pandas as pd

    return pd.Timestamp(iso_or_date).timestamp()


def _fmt_utc(epoch_s: int) -> str:
    return datetime.fromtimestamp(int(epoch_s), tz=UTC).strftime("%Y-%m-%d")


def enforce_locked_holdout(cfg: dict, windows, runner: str, allow: bool = False) -> None:
    """Raise SystemExit when the runner's test windows touch the locked period
    unless `allow` (--allow-locked) is given."""
    violations = locked_holdout_violations(cfg, windows)
    if not violations:
        return
    if allow:
        print(
            f"[journal] WARNING: {len(violations)} test window(s) overlap the "
            "locked hold-out; continuing because --allow-locked was given."
        )
        return
    raise SystemExit(
        f"[journal] LOCKED HOLD-OUT VIOLATION: {len(violations)} test window(s) of "
        f"`{runner}` overlap the reserved period "
        f"{locked_holdout_config(cfg).get('start')}..{locked_holdout_config(cfg).get('end')}. "
        "The lock exists so research never looks at that data; re-run with "
        "--allow-locked only if you accept the period is burned."
    )
