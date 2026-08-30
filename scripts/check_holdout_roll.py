"""
CI guard for the rolling hold-out lock (Rolling Holdout policy, Step 2).

config.validation.locked_holdout.start MUST equal the journal's effective current
start, derived from logs/holdout_roll_journal.csv:

    * no journal rows      -> baseline_start (config.holdout_roll.baseline_start)
    * last row MOVED        -> last.new_start
    * last row ROLLED_BACK  -> last.new_start   (the value config reverted to)
    * last row PROPOSED     -> last.old_start    (move not yet applied; config unchanged)

Drift is also caught: a config already at the proposed new_start with no MOVED
row is an out-of-band move (config edited but not journalled).

Exit 0 = consistent, 1 = drift. Intended to run as a CI job (mirrors the mypy
job in .github/workflows/ci.yml).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.holdout_roll import current_lock_start, read_journal


def expected_lock_start(cfg: dict, rows: list[dict]) -> str | None:
    """The lock start the journal implies config SHOULD currently hold."""
    baseline = str((cfg.get("holdout_roll", {}) or {}).get("baseline_start"))
    if not rows:
        return baseline
    last = rows[-1]
    dec = last.get("decision")
    if dec in ("MOVED", "ROLLED_BACK"):
        return last.get("new_start")
    if dec == "PROPOSED":
        return last.get("old_start")
    return baseline


def check_consistency(cfg: dict | None = None, rows: list[dict] | None = None) -> tuple[bool, str]:
    """Return (ok, message). ok=True means config matches the journal."""
    if cfg is None:
        cfg = load_config()
    if rows is None:
        rows = read_journal()

    config_start = current_lock_start(cfg)
    baseline = str((cfg.get("holdout_roll", {}) or {}).get("baseline_start"))

    if not rows:
        ok = config_start == baseline
        msg = (
            f"config lock start {config_start} == baseline {baseline}"
            if ok
            else f"config lock start {config_start} != baseline {baseline}"
        )
        return ok, msg

    last = rows[-1]
    expected = expected_lock_start(cfg, rows)
    if config_start == expected:
        # A PROPOSED row whose new_start already matches config means the lock
        # was moved without a MOVED journal row -> out-of-band edit.
        if last.get("decision") == "PROPOSED" and config_start == last.get("new_start"):
            return False, (
                f"config lock start {config_start} already equals proposed new_start "
                f"{last.get('new_start')} but no MOVED journal row exists"
            )
        return True, f"config lock start {config_start} matches journal effective start"
    return False, f"config lock start {config_start} != journal effective start {expected}"


def main(argv=None) -> int:
    cfg = load_config()
    ok, msg = check_consistency(cfg)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
