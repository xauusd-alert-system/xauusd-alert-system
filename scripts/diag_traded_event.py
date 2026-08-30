"""Base rates of the traded event vs the observed backtest outcome (A10).

WHAT THIS ANSWERS
-----------------
The 12-fold walk-forward (XAUUSD M15, --end-date 2026-08-08) closed 472 trades:

    breakeven scratch (post-TP1)   381   80.7%   +2390.3
    tp3_runner                      75   15.9%   +5462.0
    stop (full loss)                15    3.2%   -1431.3
    timeout                          1    0.2%      -34.7

so 96.8% of entries reached the protective level before the stop. The engine's
real geometry is TP1 = 1.0 * ATR against a stop at 2.0 * ATR (the
step_min_points / step_max_points clamps are resolved by get_signal_grid but
never read by EnsembleBacktester). For a driftless random walk the near barrier
is reached first with probability stop / (protect + stop) = 2/3 = 66.7%.

Either the 30-point gap is entry selection -- in which case the model does
something and is worth repairing -- or the unconditional rate is also ~97% and
the entire backtest result is barrier geometry with the signal contributing
nothing. The two readings lead to opposite decisions, so this measures it over
every bar in the sample instead of the 472 selected ones.

It also reports how often the two directions disagree. With the protective level
at half the stop distance, both sides usually resolve favourably inside 36 bars,
and those bars carry no information about which side to take. That share is the
ceiling on what any direction classifier could ever learn from this geometry.

Read-only: no training, no model files, nothing written except an optional CSV.

    python -m scripts.diag_traded_event --asset XAUUSD --end-date 2026-08-08
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import get_signal_grid, load_config
from labeling.label_generator import (
    generate_labels_from_config,
    generate_labels_traded_event,
    label_distribution_summary,
)
from scripts.run_backtest import (
    build_full_df,
    load_asset_history,
    truncate_before,
)

# Observed on the 12 pre-lock folds (logs/backtest_xauusd.csv, 2026-08-13).
OBSERVED = {
    "XAUUSD": {"trades": 472, "protected_pct": 96.8, "stop_pct": 3.2},
}


def _pct(part: int, whole: int) -> float:
    return float(part) / float(whole) * 100.0 if whole else float("nan")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Unconditional base rates of the traded event (A10).")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--end-date", default=None, help="Drop candles at or after this UTC date (YYYY-MM-DD).")
    parser.add_argument("--no-costs", action="store_true", help="Do not adjust the entry for half-spread + slippage.")
    parser.add_argument(
        "--keep-uneconomic", action="store_true", help="Keep events whose TP1 cannot cover the round-trip cost."
    )
    parser.add_argument("--allow-locked", action="store_true")
    parser.add_argument("--out", default=None)
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
    print(f"[te] {len(df)} {timeframe} rows for {args.asset} from {db_path}")

    if not args.allow_locked:
        from backtest.walk_forward import generate_windows
        from scripts.trial_journal import enforce_locked_holdout

        wf = cfg["backtest"]["walk_forward"]
        enforce_locked_holdout(
            cfg,
            generate_windows(df, wf["train_window_days"], wf["test_window_days"], wf["step_days"]),
            "diag_traded_event",
            allow=False,
        )

    grid = get_signal_grid(cfg, asset_cfg)
    tp1_mult = float(grid.get("tp1_mult", 1.0))
    stop_mult = float(grid.get("stop_mult", 3.0))
    be_trigger = float(grid.get("breakeven_trigger_atr", 1.0))
    protect_mult = be_trigger * tp1_mult
    horizon = int(cfg["labeling"].get("horizon_candles_n", 36))
    # Driftless first-passage benchmark (gambler's ruin): the nearer barrier is
    # reached first with probability stop / (protect + stop).
    rw_expected = 100.0 * stop_mult / (protect_mult + stop_mult)

    print(f"\n=== Traded-event base rates: {args.asset} ===")
    if args.end_date:
        print(f"Sample truncated at {args.end_date} (locked hold-out NOT touched)")
    print(
        f"Geometry: protect = {protect_mult:g} x ATR (be_trigger {be_trigger:g} x tp1 "
        f"{tp1_mult:g}) | stop = {stop_mult:g} x ATR | horizon = {horizon} bars"
    )
    print(f"Driftless random-walk expectation: {rw_expected:.1f}% reach protect first")

    kw = dict(
        horizon_n=horizon,
        include_costs=not args.no_costs,
        require_net_positive=not args.keep_uneconomic,
    )
    # Each side is scanned once; the direction label is derived here rather than
    # via traded_event_summary, which would repeat both scans.
    long_lab = generate_labels_traded_event(df, cfg, args.asset, direction=1, **kw)
    short_lab = generate_labels_traded_event(df, cfg, args.asset, direction=-1, **kw)

    rows = len(df)
    out_rows = []
    for name, lab in (("long", long_lab), ("short", short_lab)):
        v = lab.dropna()
        fav = int((v == 1.0).sum())
        out_rows.append(
            {
                "side": name,
                "resolved": len(v),
                "unresolved": int(lab.isna().sum()),
                "protect_first": fav,
                "stop_first": int((v == 0.0).sum()),
                "protect_first_pct": round(_pct(fav, len(v)), 2),
            }
        )

    hdr = f"{'side':<7}{'resolved':>10}{'unresolved':>12}{'protect':>9}{'stop':>8}{'protect%':>10}"
    print("\n1. UNCONDITIONAL OUTCOME PER SIDE (every bar, no signal filter)")
    print(hdr)
    print("-" * len(hdr))
    for r in out_rows:
        print(
            f"{r['side']:<7}{r['resolved']:>10}{r['unresolved']:>12}"
            f"{r['protect_first']:>9}{r['stop_first']:>8}{r['protect_first_pct']:>10.2f}"
        )

    obs = OBSERVED.get(args.asset)
    if obs:
        print(
            f"\n   observed on {obs['trades']} selected entries: {obs['protected_pct']:.1f}% protected, "
            f"{obs['stop_pct']:.1f}% full stops"
        )
        short_rate = out_rows[1]["protect_first_pct"]
        edge = short_rate - obs["protected_pct"]
        print(
            f"   short side unconditional {short_rate:.2f}% vs selected {obs['protected_pct']:.1f}% "
            f"-> selection effect {edge:+.2f} pp"
        )
        if abs(edge) < 2.0:
            print("   -> the signal selected nothing: the result is barrier geometry.")
        elif edge < -2.0:
            print("   -> selected entries did BETTER than average; selection has some content.")
        else:
            print("   -> selected entries did WORSE than average; the signal is harmful.")

    # --- 2. is there anything to predict about direction? -----------------
    l = long_lab.values
    s = short_lab.values
    resolved_both = (~np.isnan(l)) & (~np.isnan(s))
    long_better = resolved_both & (l == 1.0) & (s == 0.0)
    short_better = resolved_both & (l == 0.0) & (s == 1.0)
    both_ok = resolved_both & (l == 1.0) & (s == 1.0)
    both_bad = resolved_both & (l == 0.0) & (s == 0.0)
    defined = int(long_better.sum() + short_better.sum())

    print("\n2. DIRECTIONAL INFORMATION")
    print(f"   both sides protected      : {int(both_ok.sum()):>7}  ({_pct(int(both_ok.sum()), rows):.2f}% of bars)")
    print(f"   both sides stopped        : {int(both_bad.sum()):>7}  ({_pct(int(both_bad.sum()), rows):.2f}%)")
    print(f"   long better than short    : {int(long_better.sum()):>7}  ({_pct(int(long_better.sum()), rows):.2f}%)")
    print(f"   short better than long    : {int(short_better.sum()):>7}  ({_pct(int(short_better.sum()), rows):.2f}%)")
    print(f"   -> directionally informative bars: {defined} ({_pct(defined, rows):.2f}% of sample)")
    if defined:
        print(
            f"   -> class balance of that subset: long {_pct(int(long_better.sum()), defined):.2f}% / "
            f"short {_pct(int(short_better.sum()), defined):.2f}%"
        )
        print("      (any deviation from 50% becomes a permanent live bias, because the")
        print("       ensemble compares p against absolute constants 0.55 / 0.62 / 0.71)")
    if _pct(defined, rows) < 20.0:
        print("   VERDICT: the geometry is near direction-agnostic. A classifier cannot")
        print("            pick a side here; the fix is the geometry, not the model.")

    # --- 3. contrast with the label actually used for training ------------
    print("\n3. LABEL CURRENTLY USED FOR TRAINING (triple barrier, for contrast)")
    try:
        old = df["label"] if "label" in df.columns else generate_labels_from_config(df, cfg)
        summ = label_distribution_summary(old)
        if summ.get("total_valid"):
            print(
                f"   valid={summ['total_valid']} nan={summ['nan_count']} "
                f"upper={summ['pct_upper_hit']:.2f}% lower={summ['pct_lower_hit']:.2f}% "
                f"no_hit={summ['pct_no_hit']:.2f}%"
            )
            print("   The model is trained to predict THIS, then executed against the")
            print("   geometry measured in section 1. Those are different events.")
        else:
            print("   no valid rows")
    except Exception as exc:
        print(f"   could not compute the legacy label: {exc}")

    # --- 4. breakdown by regime (short side: the side actually traded) ----
    if "regime" in df.columns:
        print("\n4. SHORT-SIDE PROTECT RATE BY REGIME (the side that traded 96.4% of the time)")
        tmp = pd.DataFrame({"regime": df["regime"].astype(str).values, "lab": s})
        tmp = tmp.dropna(subset=["lab"])
        rhdr = f"{'regime':<20}{'n':>9}{'protect%':>10}"
        print(rhdr)
        print("-" * len(rhdr))
        for reg, grp in tmp.groupby("regime"):
            print(f"{reg:<20}{len(grp):>9}{100.0 * (grp['lab'] == 1.0).mean():>10.2f}")

    os.makedirs("logs", exist_ok=True)
    out_csv = args.out or f"logs/traded_event_{args.asset.lower()}.csv"
    pd.DataFrame(out_rows).to_csv(out_csv, index=False)
    print(f"\n[te] CSV -> {out_csv}")


if __name__ == "__main__":
    main()
