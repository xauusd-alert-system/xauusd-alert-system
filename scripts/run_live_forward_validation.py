"""One-time validation read of a frozen append-only paper ledger.

This command refuses to expose outcomes before the pre-registered closed-trade
minimum.  Once the gate is reached it appends ``validation_read`` BEFORE reading
PnL payloads, permanently recording that the hold-out has been consumed.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from backtest.deflated_sharpe import deflated_sharpe_ratio
from backtest.metrics import block_bootstrap_t
from data.paper_ledger import (
    append_paper_event,
    paper_accumulation_status,
    read_paper_events,
)
from paper.accumulator import load_frozen_manifest


def check_thresholds(trial: dict, min_trades: int) -> dict:
    checks = {
        f"n_trades >= {min_trades}": trial.get("n_trades", 0) >= min_trades,
        "PF >= 1.30": trial.get("profit_factor", 0.0) >= 1.30,
        "cost_x1_5_pf >= 1.20": trial.get("cost_x1_5_pf", 0.0) >= 1.20,
        "t_block >= 1.50": trial.get("t_block", float("nan")) >= 1.50,
        "DSR(N_eff) >= 0.80": trial.get("dsr_neff", float("nan")) >= 0.80,
    }
    checks["passed_all"] = all(checks.values())
    return checks


def _profit_factor(values: np.ndarray) -> float:
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    return gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)


def trial_from_closed_events(closed: pd.DataFrame, historical_trials: int = 737) -> dict:
    payloads = list(closed["payload"])
    pnl = np.asarray([float(p["pnl"]) for p in payloads], dtype=float)
    costs = np.asarray([float(p["execution_cost_money"]) for p in payloads], dtype=float)
    r = np.asarray([float(p.get("r_multiple", 0.0)) for p in payloads], dtype=float)
    stressed = pnl - 0.5 * costs
    dsr = deflated_sharpe_ratio(pnl, n_trials=historical_trials, t_eff=float(len(pnl)))
    return {
        "n_trades": int(len(pnl)),
        "total_pnl": float(pnl.sum()),
        "profit_factor": float(_profit_factor(pnl)),
        "cost_x1_5_total_pnl": float(stressed.sum()),
        "cost_x1_5_pf": float(_profit_factor(stressed)),
        "t_block": float(block_bootstrap_t(r, block=20, n_boot=10000, seed=42)),
        "dsr_neff": float(dsr["dsr"]),
        "effective_n": float(dsr["t_eff"]),
        "historical_trials": int(historical_trials),
        "period_start_timestamp_utc": int(closed["bar_timestamp_utc"].min()),
        "period_end_timestamp_utc": int(closed["bar_timestamp_utc"].max()),
        "source": "frozen_append_only_paper_ledger",
        "synthetic": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-time frozen live-forward validation.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--paper-db", default="data/paper_forward.sqlite")
    parser.add_argument(
        "--force", action="store_true",
        help="Explicitly confirm the single irreversible hold-out outcome read.",
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    # Model is not needed to read already accumulated outcomes; the manifest and
    # config hashes are still verified. This permits validation after archival.
    manifest = load_frozen_manifest(args.manifest, verify_model=False)
    run_id = manifest["run_id"]
    status = paper_accumulation_status(args.paper_db, run_id)
    minimum = int(manifest["min_closed_trades"])
    if status["closed_trades"] < minimum:
        raise SystemExit(
            f"HOLD-OUT SEALED: {status['closed_trades']}/{minimum} closed paper trades. "
            "No outcomes were read; keep accumulating."
        )
    if status["validation_reads"]:
        raise SystemExit(
            "HOLD-OUT ALREADY READ: validation_read exists for this frozen run; "
            "refusing a sequential second look."
        )
    if not args.force:
        raise SystemExit(
            "HOLD-OUT READY BUT SEALED: re-run with --force to confirm the single "
            "irreversible outcome read."
        )

    # Burn marker first. A crash after this line still counts as a consumed read.
    marker_created = append_paper_event(
        args.paper_db, run_id=run_id, event_type="validation_read",
        idempotency_key=f"{run_id}:validation_read:1",
        event_timestamp_utc=int(pd.Timestamp.now(tz="UTC").timestamp()),
        payload={
            "manifest_sha256": manifest["manifest_sha256"],
            "closed_trades_at_read": status["closed_trades"],
            "thresholds": {
                "min_trades": minimum, "profit_factor": 1.30,
                "cost_x1_5_pf": 1.20, "t_block": 1.50, "dsr_neff": 0.80,
            },
        },
    )
    if not marker_created:
        # A concurrent validator won the unique idempotency-key race. Do not let
        # both processes inspect outcomes after observing the same pre-marker status.
        raise SystemExit("HOLD-OUT ALREADY READ: concurrent validation marker exists.")
    closed = read_paper_events(args.paper_db, run_id, event_type="close")
    trial = trial_from_closed_events(closed)
    trial.update({
        "run_id": run_id,
        "asset_key": manifest["asset_key"],
        "variant": manifest["variant"],
        "manifest_sha256": manifest["manifest_sha256"],
        "model_sha256": manifest["model_sha256"],
    })
    checks = check_thresholds(trial, minimum)
    result = {"trial": trial, "checks": checks}

    print(json.dumps(result, indent=2, sort_keys=True))
    print("PROMOTE CANDIDATE READY" if checks["passed_all"] else "NOT READY — remain paper")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")


if __name__ == "__main__":
    main()
