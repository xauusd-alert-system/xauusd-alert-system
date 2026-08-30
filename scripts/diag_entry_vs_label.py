"""Reconcile the engine's exit reasons with an independent barrier recomputation.

WHY THIS EXISTS
---------------
Two numbers measured on the same asset, same sample, same barrier geometry:

    engine (472 selected entries)      : 96.8% protected, 3.2% full stops
    unconditional short side, all bars : 62.73% protected

A +34 percentage-point lift means the entry filter reduced the failure rate by a
factor of ~11.6. The filter is a model with walk-forward AUC 0.5233 and a
probability spread of p_std ~ 0.008. Those two facts cannot both be true of the
same signal, so at least one of the two numbers is not measuring the event its
name suggests.

There are exactly three candidate explanations and this script separates them:

1. BOOKKEEPING. The engine's `breakeven` exit reason fires on `tp1_hit or
   be_triggered`, and 80.7% of all exits carry it. If the engine's notion of
   "protected" is reached through a path the plain barrier scan does not
   reproduce, then 96.8% is an artefact of exit accounting, not a property of
   the market. Tested here by reading the independent label at the very same
   signal bar and printing the confusion matrix.
2. FILTER COMPOSITION. The ensemble suppresses `asia` / `off_session` and the
   `compression` / `reversal_watch` regimes, so the traded bars are not a random
   sample of all bars. Tested here with a control group restricted to the test
   windows and reweighted to the session/regime mix of the actual entries.
3. CLUSTERING. The 472 trades are spread over 12 folds as
   0/0/0/0/0/1/6/18/25/68/93/103/215/240-style bursts. If they collapse onto a
   handful of days, the effective sample size is single digits and 96.8% carries
   no statistical weight at all. Tested here by day-level aggregation.

HONESTY
-------
The per-fold models, windows and purge gap are imported from
scripts.deflated_sharpe, so they are bit-identical to the runs being explained.
The independent label comes from labeling.label_generator, which reconstructs
the engine's geometry from the same config but shares no code with the engine.
Models are trained into temp files; the production model is never touched.
Nothing is written except an optional CSV.

Usage:

    python -m scripts.diag_entry_vs_label --asset XAUUSD --end-date 2026-08-08
"""

import argparse
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from labeling.label_generator import generate_labels_traded_event
from model.ensemble_backtest import EnsembleBacktester
from scripts.deflated_sharpe import _build_fold_frames
from scripts.run_backtest import (
    build_full_df,
    load_asset_history,
    merge_asset_cfg,
    truncate_before,
)


def _epoch_array(values) -> np.ndarray:
    """Epoch seconds as int64, accepting either numeric or datetime input."""
    arr = np.asarray(values)
    if arr.dtype.kind in "iuf":
        return arr.astype("int64")
    return (pd.to_datetime(pd.Series(arr), utc=True).astype("int64") // 10**9).to_numpy()


def _epoch_scalar(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(pd.Timestamp(value).timestamp())


def _engine_outcome(trade) -> str:
    """Collapse the engine's exit reason onto the barrier question.

    `exit_reason == "stop"` is only assigned when neither tp1_hit nor
    be_triggered ever fired, i.e. a genuine full stop. Everything else means the
    protective level was reached first. `timeout` is neither and is kept apart.
    """
    reason = str(trade.exit_reason)
    if reason == "stop":
        return "stop"
    if reason == "timeout":
        return "timeout"
    return "protect"


def _label_outcome(value) -> str:
    if value is None:
        return "nan"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "nan"
    if not np.isfinite(v):
        return "nan"
    return "protect" if v >= 0.5 else "stop"


def _group_table(labels: pd.Series, keys: pd.Series) -> pd.DataFrame:
    """Resolved / protect counts of a label Series grouped by a key Series."""
    frame = pd.DataFrame({"key": keys.astype(str).to_numpy(), "lab": labels.to_numpy(dtype=float)})
    frame = frame[np.isfinite(frame["lab"])]
    if frame.empty:
        return pd.DataFrame(columns=["key", "n", "protect_pct"])
    grp = frame.groupby("key")["lab"].agg(["size", "mean"]).reset_index()
    grp.columns = ["key", "n", "protect_pct"]
    grp["protect_pct"] = 100.0 * grp["protect_pct"]
    return grp.sort_values("key").reset_index(drop=True)


def _reweighted(control: pd.DataFrame, entry_mix: Counter) -> float:
    """Control protect% reweighted to the composition of the traded entries."""
    rates = {str(r["key"]): float(r["protect_pct"]) for _, r in control.iterrows()}
    total = sum(entry_mix.values())
    if not total:
        return float("nan")
    acc = 0.0
    covered = 0
    for key, cnt in entry_mix.items():
        if key in rates:
            acc += rates[key] * cnt
            covered += cnt
    return acc / covered if covered else float("nan")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check the engine's protect rate against an independent barrier scan.")
    parser.add_argument("--asset", required=True, help="Internal asset key (XAUUSD, ...)")
    parser.add_argument("--timeframe", default=None, help="Override timeframe (default: per-asset)")
    parser.add_argument("--db-path", default=None, help="SQLite DB (default: config general.db_path)")
    parser.add_argument(
        "--end-date",
        default=None,
        help="Drop candles at or after this UTC date (YYYY-MM-DD) before building "
        "features. Same semantics as scripts/run_backtest.py --end-date.",
    )
    parser.add_argument("--max-folds", type=int, default=None, help="Cap folds (quick runs)")
    parser.add_argument(
        "--allow-locked", action="store_true", help="Allow test windows overlapping the locked hold-out"
    )
    parser.add_argument("--out", default=None, help="Output CSV path (default: logs/entry_vs_label_<asset>.csv)")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.asset not in cfg.get("assets", {}):
        raise SystemExit(f"Unknown asset: {args.asset}")
    asset_cfg = cfg["assets"][args.asset]
    timeframe = args.timeframe or asset_cfg.get("timeframe") or "M5"
    db_path = args.db_path or cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    raw = load_asset_history(db_path, timeframe, args.asset)
    if args.end_date:
        raw = truncate_before(raw, args.end_date, args.asset)
    df = build_full_df(cfg, raw, db_path=db_path, asset_key=args.asset)
    print(f"[evl] {len(df)} {timeframe} rows for {args.asset} from {db_path}")

    from backtest.walk_forward import generate_windows
    from scripts.trial_journal import enforce_locked_holdout

    wf = cfg["backtest"]["walk_forward"]
    enforce_locked_holdout(
        cfg,
        generate_windows(df, wf["train_window_days"], wf["test_window_days"], wf["step_days"]),
        "diag_entry_vs_label",
        allow=args.allow_locked,
    )

    windows, frames = _build_fold_frames(df, cfg, args.asset, args.max_folds)

    cfg_run = merge_asset_cfg(cfg, args.asset, "labeling")
    cfg_run = merge_asset_cfg(cfg_run, args.asset, "ensemble")

    rows: list[dict] = []
    control_parts: list[pd.DataFrame] = []
    unmatched = 0

    for i, fdf in enumerate(frames):
        fdf = fdf.reset_index(drop=True)
        engine = EnsembleBacktester(cfg_run, asset_key=args.asset)
        trades = engine.run(fdf.copy())
        if not trades:
            print(f"[evl] fold {i + 1}: no trades")
            continue

        # Independent barrier scan on the same frame, both sides.
        labels = {
            1: generate_labels_traded_event(fdf, cfg_run, asset_key=args.asset, direction=1),
            -1: generate_labels_traded_event(fdf, cfg_run, asset_key=args.asset, direction=-1),
        }

        epochs = _epoch_array(fdf["timestamp_utc"])
        pos = {int(ts): k for k, ts in enumerate(epochs)}
        sess = fdf["session"].astype(str) if "session" in fdf.columns else pd.Series(["n/a"] * len(fdf))
        reg = fdf["regime"].astype(str) if "regime" in fdf.columns else pd.Series(["n/a"] * len(fdf))

        control_parts.append(
            pd.DataFrame(
                {
                    "fold": i + 1,
                    "session": sess.to_numpy(),
                    "regime": reg.to_numpy(),
                    "lab_short": labels[-1].to_numpy(dtype=float),
                    "lab_long": labels[1].to_numpy(dtype=float),
                }
            )
        )

        for t in trades:
            key = _epoch_scalar(t.entry_ts)
            entry_idx = pos.get(key)
            if entry_idx is None or entry_idx < 1:
                unmatched += 1
                continue
            # The label lives on the SIGNAL bar; the engine fills at the open of
            # the following bar, so the signal bar is one index earlier.
            sig_idx = entry_idx - 1
            lab = labels[int(t.direction)].iloc[sig_idx]
            rows.append(
                {
                    "fold": i + 1,
                    "entry_ts": key,
                    "day": pd.Timestamp(key, unit="s", tz="UTC").strftime("%Y-%m-%d"),
                    "direction": int(t.direction),
                    "session": str(sess.iloc[sig_idx]),
                    "regime": str(reg.iloc[sig_idx]),
                    "exit_reason": str(t.exit_reason),
                    "engine": _engine_outcome(t),
                    "label": _label_outcome(lab),
                    "pnl": round(float(t.pnl), 2),
                }
            )

    if not rows:
        raise SystemExit("[evl] no trades matched a frame bar; nothing to compare")

    rep = pd.DataFrame(rows)
    control = pd.concat(control_parts, ignore_index=True) if control_parts else pd.DataFrame()

    print(f"\n=== Engine vs independent barrier scan: {args.asset} ===")
    if args.end_date:
        print(f"Sample truncated at {args.end_date} (locked hold-out NOT touched)")
    if unmatched:
        print(f"WARNING: {unmatched} trade(s) could not be matched to a frame bar")
    print(
        f"trades compared: {len(rep)}  "
        f"(long {int((rep['direction'] == 1).sum())} / short {int((rep['direction'] == -1).sum())})"
    )

    # --- 1. same bars, two independent verdicts ---------------------------
    print("\n1. CONFUSION MATRIX ON THE SAME BARS (rows = engine, cols = label)")
    order_e = ["protect", "stop", "timeout"]
    order_l = ["protect", "stop", "nan"]
    hdr = "engine\\label".ljust(14) + "".join(f"{c:>10}" for c in order_l) + f"{'total':>10}"
    print(hdr)
    print("-" * len(hdr))
    for e in order_e:
        sub = rep[rep["engine"] == e]
        line = f"{e:<14}"
        for l in order_l:
            line += f"{int((sub['label'] == l).sum()):>10}"
        line += f"{len(sub):>10}"
        print(line)
    print("-" * len(hdr))
    line = f"{'total':<14}"
    for l in order_l:
        line += f"{int((rep['label'] == l).sum()):>10}"
    print(line + f"{len(rep):>10}")

    eng_resolved = rep[rep["engine"].isin(["protect", "stop"])]
    lab_resolved = rep[rep["label"].isin(["protect", "stop"])]
    eng_pct = 100.0 * float((eng_resolved["engine"] == "protect").mean()) if len(eng_resolved) else float("nan")
    lab_pct = 100.0 * float((lab_resolved["label"] == "protect").mean()) if len(lab_resolved) else float("nan")
    both = rep[(rep["engine"] != "timeout") & (rep["label"] != "nan")]
    agree = 100.0 * float((both["engine"] == both["label"]).mean()) if len(both) else float("nan")
    print(f"\n   engine protect% (resolved n={len(eng_resolved)}) = {eng_pct:.2f}")
    print(f"   label  protect% (resolved n={len(lab_resolved)}) = {lab_pct:.2f}")
    print(f"   per-trade agreement on n={len(both)} jointly resolved = {agree:.2f}%")
    gap = eng_pct - lab_pct
    print(f"   gap = {gap:+.2f} pp")
    if np.isfinite(gap):
        if abs(gap) <= 5.0:
            print("   -> the two measurements agree: 96.8% is a real property of the")
            print("      selected bars, so the explanation must be in sections 2 and 3.")
        else:
            print("   -> the two measurements DISAGREE on the same bars. The engine's")
            print("      protect rate is then a product of exit bookkeeping, not of the")
            print("      barrier geometry, and every PnL figure built on it is suspect.")

    # --- 2. matched control group ----------------------------------------
    print("\n2. CONTROL GROUP INSIDE THE TEST WINDOWS ONLY (no signal filter)")
    if not control.empty:
        for side, col, dirn in (("short", "lab_short", -1), ("long", "lab_long", 1)):
            res = control[np.isfinite(control[col])]
            pct = 100.0 * float(res[col].mean()) if len(res) else float("nan")
            print(f"   {side:<6} unconditional protect% = {pct:.2f}  (resolved n={len(res)})")
        traded_side = -1 if int((rep["direction"] == -1).sum()) >= int((rep["direction"] == 1).sum()) else 1
        col = "lab_short" if traded_side == -1 else "lab_long"
        sub = rep[rep["direction"] == traded_side]
        for dim in ("session", "regime"):
            table = _group_table(control[col], control[dim])
            if table.empty:
                continue
            print(f"\n   {dim} breakdown of the control group (side {traded_side:+d}):")
            print(f"   {'key':<22}{'n':>8}{'protect%':>11}{'entry n':>10}")
            mix = Counter(sub[dim].astype(str).tolist())
            for _, r in table.iterrows():
                print(
                    f"   {str(r['key']):<22}{int(r['n']):>8}{float(r['protect_pct']):>11.2f}"
                    f"{mix.get(str(r['key']), 0):>10}"
                )
            exp = _reweighted(table, mix)
            print(f"   -> control reweighted to the entry {dim} mix = {exp:.2f}%")
            print(f"      (compare with the label protect% of {lab_pct:.2f} on the entries)")
    else:
        print("   no control data")

    # --- 3. clustering ----------------------------------------------------
    print("\n3. CLUSTERING OF THE ENTRIES (effective sample size)")
    per_day = rep.groupby("day").size().sort_values(ascending=False)
    print(f"   distinct calendar days with entries : {len(per_day)}")
    print(f"   trades on the busiest day           : {int(per_day.iloc[0])}")
    top5 = int(per_day.iloc[:5].sum())
    print(f"   share of trades on the top 5 days   : {100.0 * top5 / len(rep):.1f}%")
    print(f"   distinct folds with entries         : {rep['fold'].nunique()}")
    day_lab = rep[rep["label"] != "nan"].copy()
    if len(day_lab):
        day_lab["ok"] = (day_lab["label"] == "protect").astype(float)
        day_rates = day_lab.groupby("day")["ok"].mean()
        print(f"   day-level mean protect% (equal weight per day) = {100.0 * float(day_rates.mean()):.2f}")
        print(f"   days with a protect rate below 50%             = {int((day_rates < 0.5).sum())} of {len(day_rates)}")
    print("   -> trades inside one day share the same 36-bar horizon and the same")
    print("      move, so they are not independent draws. Read the day count, not")
    print(f"      the {len(rep)} trade count, as the sample size.")

    os.makedirs("logs", exist_ok=True)
    out_csv = args.out or f"logs/entry_vs_label_{args.asset.lower()}.csv"
    rep.to_csv(out_csv, index=False)
    print(f"\n[evl] CSV -> {out_csv}")


if __name__ == "__main__":
    main()
