"""Direction / beta / exit-policy decomposition of the ensemble backtest.

WHY THIS EXISTS
---------------
The deflated-Sharpe run over the 12 pre-lock folds (2026-08-13, XAUUSD M15,
--end-date 2026-08-08) reported:

    variant   n_tr     PnL   WR%    PF   DSR(Nef)  +folds
    current    472  6386.4  34.1  4.19       0.87    6/12
    null       922 10202.9  48.8  1.45       0.84   12/12

`null` is the negative control: random 0.5+/-0.05 probabilities, no model at
all. It traded twice as often as the shipped config, earned more money, and was
profitable in every single fold. A coin flip cannot out-perform a signal unless
the money is coming from somewhere other than the signal.

There are exactly three places it can come from, and the metrics logged today
cannot tell them apart:

1. DIRECTION BIAS x MARKET DRIFT. If the engine is structurally net long and
   gold rose across the sample, the PnL is beta. Measured here as the per-fold
   long/short split next to a buy-and-hold benchmark computed on the SAME test
   window with the SAME lot size and contract multiplier, plus the correlation
   between fold PnL and that benchmark.
2. EXIT POLICY. On TP1 the stop moves to entry, so most would-be losers book a
   scratch worth roughly the round-trip cost instead of a full stop. Measured
   here as an exit-reason histogram with PnL attribution.
3. SIGNAL. Whatever is left once 1 and 2 are accounted for.

HONESTY
-------
The per-fold scoring path is imported from scripts.deflated_sharpe, so the
models, windows and purge gap are bit-identical to the run being explained.
Models are trained into temp files; the production model is never touched.
Nothing is written except an optional CSV. The locked hold-out is enforced the
same way as in the other runners, and --end-date has the same semantics.

Usage:

    python -m scripts.diag_direction_beta --asset XAUUSD --end-date 2026-08-08
"""

import argparse
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.run_backtest import (
    load_asset_history,
    build_full_df,
    merge_asset_cfg,
    truncate_before,
)
from scripts.deflated_sharpe import _build_fold_frames
from model.ensemble_backtest import EnsembleBacktester


def _pnl(trades) -> float:
    return float(sum(float(t.pnl) for t in trades)) if trades else 0.0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Split backtest PnL into direction bias, market beta and exit policy.")
    parser.add_argument("--asset", required=True, help="Internal asset key (XAUUSD, ...)")
    parser.add_argument("--timeframe", default=None, help="Override timeframe (default: per-asset)")
    parser.add_argument("--db-path", default=None, help="SQLite DB (default: config general.db_path)")
    parser.add_argument("--end-date", default=None,
                        help="Drop candles at or after this UTC date (YYYY-MM-DD) before building "
                             "features. Same semantics as scripts/run_backtest.py --end-date.")
    parser.add_argument("--max-folds", type=int, default=None, help="Cap folds (quick runs)")
    parser.add_argument("--allow-locked", action="store_true",
                        help="Allow test windows overlapping the locked hold-out")
    parser.add_argument("--out", default=None,
                        help="Output CSV path (default: logs/direction_beta_<asset>.csv)")
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
    print(f"[dir] {len(df)} {timeframe} rows for {args.asset} from {db_path}")

    from backtest.walk_forward import generate_windows
    from scripts.trial_journal import enforce_locked_holdout

    wf = cfg["backtest"]["walk_forward"]
    enforce_locked_holdout(
        cfg,
        generate_windows(df, wf["train_window_days"], wf["test_window_days"], wf["step_days"]),
        "diag_direction_beta",
        allow=args.allow_locked,
    )

    # Same per-fold models / windows / purge as the DSR run being explained.
    windows, frames = _build_fold_frames(df, cfg, args.asset, args.max_folds)

    cfg_run = merge_asset_cfg(cfg, args.asset, "labeling")
    cfg_run = merge_asset_cfg(cfg_run, args.asset, "ensemble")

    rows = []
    reason_n: Counter = Counter()
    reason_pnl: dict = {}

    for i, fdf in enumerate(frames):
        engine = EnsembleBacktester(cfg_run, asset_key=args.asset)
        trades = engine.run(fdf.reset_index(drop=True))

        longs = [t for t in trades if t.direction == 1]
        shorts = [t for t in trades if t.direction == -1]

        for t in trades:
            key = str(t.exit_reason)
            reason_n[key] += 1
            reason_pnl[key] = reason_pnl.get(key, 0.0) + float(t.pnl)

        closes = fdf["close"].to_numpy(dtype=float)
        # Buy-and-hold over the same window, same lot, same contract multiplier,
        # so the number is directly comparable with the strategy PnL.
        buy_hold = (float(closes[-1] - closes[0]) * engine.volume * engine.point_value_lot
                    if len(closes) >= 2 else 0.0)

        rows.append({
            "fold": i + 1,
            "n_trades": len(trades),
            "n_long": len(longs),
            "n_short": len(shorts),
            "long_share_pct": round(100.0 * len(longs) / len(trades), 1) if trades else 0.0,
            "pnl_long": round(_pnl(longs), 2),
            "pnl_short": round(_pnl(shorts), 2),
            "pnl_total": round(_pnl(trades), 2),
            "buy_hold": round(buy_hold, 2),
        })

    rep = pd.DataFrame(rows)

    print(f"\n=== Direction / beta decomposition: {args.asset} ===")
    if args.end_date:
        print(f"Sample truncated at {args.end_date} (locked hold-out NOT touched)")
    hdr = (f"{'fold':>5}{'n_tr':>6}{'n_long':>8}{'n_short':>8}{'long%':>7}"
           f"{'pnl_long':>11}{'pnl_short':>11}{'pnl_tot':>11}{'buy_hold':>11}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['fold']:>5}{r['n_trades']:>6}{r['n_long']:>8}{r['n_short']:>8}"
              f"{r['long_share_pct']:>7.1f}{r['pnl_long']:>11.1f}{r['pnl_short']:>11.1f}"
              f"{r['pnl_total']:>11.1f}{r['buy_hold']:>11.1f}")

    tot_long = int(rep["n_long"].sum())
    tot_short = int(rep["n_short"].sum())
    tot_n = tot_long + tot_short
    print("-" * len(hdr))
    print(f"{'ALL':>5}{tot_n:>6}{tot_long:>8}{tot_short:>8}"
          f"{(100.0 * tot_long / tot_n if tot_n else 0.0):>7.1f}"
          f"{rep['pnl_long'].sum():>11.1f}{rep['pnl_short'].sum():>11.1f}"
          f"{rep['pnl_total'].sum():>11.1f}{rep['buy_hold'].sum():>11.1f}")

    # --- 1. beta ----------------------------------------------------------
    traded = rep[rep["n_trades"] > 0]
    if len(traded) >= 3 and traded["buy_hold"].std() > 0 and traded["pnl_total"].std() > 0:
        corr = float(np.corrcoef(traded["pnl_total"], traded["buy_hold"])[0, 1])
        corr_s = f"{corr:+.3f}"
    else:
        corr = float("nan")
        corr_s = "n/a (too few traded folds)"
    print(f"\n1. MARKET BETA")
    print(f"   corr(fold PnL, buy-and-hold) = {corr_s}")
    if np.isfinite(corr):
        if corr >= 0.6:
            print("   -> fold results track the market: the PnL is largely BETA, not edge.")
        elif corr >= 0.3:
            print("   -> partial market dependence; edge is not cleanly separated from drift.")
        else:
            print("   -> fold results are not explained by market drift.")
    long_share = 100.0 * tot_long / tot_n if tot_n else 0.0
    print(f"   long share = {long_share:.1f}% of {tot_n} trades "
          f"({'directionally biased' if abs(long_share - 50.0) >= 10.0 else 'roughly balanced'})")

    # --- 2. exit policy ---------------------------------------------------
    print(f"\n2. EXIT POLICY (where the money is actually booked)")
    ehdr = f"{'exit_reason':<16}{'n':>7}{'share%':>9}{'pnl':>12}{'avg':>10}"
    print(ehdr)
    print("-" * len(ehdr))
    grand_n = sum(reason_n.values())
    grand_pnl = sum(reason_pnl.values())
    for reason, n in reason_n.most_common():
        pnl = reason_pnl.get(reason, 0.0)
        print(f"{reason:<16}{n:>7}{(100.0 * n / grand_n if grand_n else 0.0):>9.1f}"
              f"{pnl:>12.1f}{(pnl / n if n else 0.0):>10.2f}")
    print("-" * len(ehdr))
    print(f"{'TOTAL':<16}{grand_n:>7}{100.0:>9.1f}{grand_pnl:>12.1f}"
          f"{(grand_pnl / grand_n if grand_n else 0.0):>10.2f}")
    scratch = reason_n.get("breakeven", 0)
    if grand_n:
        print(f"   breakeven scratches = {100.0 * scratch / grand_n:.1f}% of all exits "
              f"(these are created by the TP1 stop-to-entry move, not by the signal)")

    os.makedirs("logs", exist_ok=True)
    out_csv = args.out or f"logs/direction_beta_{args.asset.lower()}.csv"
    rep.to_csv(out_csv, index=False)
    print(f"\n[dir] CSV -> {out_csv}")


if __name__ == "__main__":
    main()
