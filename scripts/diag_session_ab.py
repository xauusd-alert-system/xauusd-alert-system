"""One-off A/B: isolate the 22:00-23:59 UTC session-relabel effect on XAUUSD
walk-forward session metrics.

Control:  session labels as stored in the DB (22-23h UTC = ``off_session``,
          the post-fix state).
Experiment: the SAME true-UTC DB, but bars at hours 22-23 are forced back to
          ``newyork`` (the pre-relabel state), so the only difference between
          the two runs is the session label of ~4,472 XAUUSD M15 bars.

Both runs use the identical honest harness (train_mt5.build_full_df + shared
fold frames + collect_direction_records, variant 'current', end 2026-08-08).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import _variants_for
from scripts.diag_direction_split import collect_direction_records, _metrics_for
from scripts.run_backtest import load_asset_history, truncate_before
from scripts.train_mt5 import build_full_df

ASSET = "XAUUSD"
END_DATE = "2026-08-08"
VARIANT = "current"


def _session_metrics(rec: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for session, sub in rec.groupby("session", observed=True):
        m = _metrics_for(sub)
        rows.append({
            "session": session, "n": int(m["n"]), "WR%": float(m["WR%"]),
            "PF": float(m["PF"]), "sum_R": float(m["sum_R"]),
        })
    return pd.DataFrame(rows)


def _run(label: str, force_2223_newyork: bool) -> pd.DataFrame:
    cfg = load_config()
    db = cfg.get("general", {}).get("db_path")
    timeframe = cfg["assets"][ASSET].get("timeframe", "M15")

    raw = load_asset_history(db, timeframe, ASSET)
    if force_2223_newyork:
        ts = pd.to_datetime(raw["timestamp_utc"], unit="s", utc=True)
        mask = (ts.dt.weekday < 5) & (ts.dt.hour.isin([22, 23]))
        n = int(mask.sum())
        raw.loc[mask, "session"] = "newyork"
        print(f"[{label}] forced {n} weekday 22-23h bars -> newyork", flush=True)
    raw = truncate_before(raw, END_DATE, ASSET)
    df_full = build_full_df(raw, cfg, db_path=db, asset_key=ASSET, timeframe=timeframe)

    overrides = _variants_for(ASSET)[VARIANT]
    records = collect_direction_records(cfg, ASSET, df_full, VARIANT, overrides)
    rec = pd.DataFrame(records) if records else pd.DataFrame()
    print(f"[{label}] trades={len(rec)} total_sumR={_metrics_for(rec)['sum_R']}", flush=True)
    return rec


def main() -> None:
    control = _run("control (stored: 22-23h=off_session)", force_2223_newyork=False)
    experiment = _run("experiment (22-23h forced to newyork)", force_2223_newyork=True)

    print("\n=== SESSION METRICS — control (post-fix labels) ===")
    print(_session_metrics(control).to_string(index=False))
    print("\n=== SESSION METRICS — experiment (22-23h = newyork, pre-relabel) ===")
    print(_session_metrics(experiment).to_string(index=False))

    out = os.path.join("logs", "session_ab.csv")
    pd.concat([
        _session_metrics(control).assign(run="control"),
        _session_metrics(experiment).assign(run="experiment"),
    ]).to_csv(out, index=False)
    print(f"\nWROTE {out}")


if __name__ == "__main__":
    main()
