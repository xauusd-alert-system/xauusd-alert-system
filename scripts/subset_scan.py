"""
Subset-scan with multiple-testing correction for walk-forward backtest results.

Scans all subsets of trades by categorical columns (regime, session, direction,
exit_path, etc.) and applies Bonferroni / DSR correction to detect genuinely
significant edge vs noise mining.

Usage as a library:
    from scripts.subset_scan import SubsetScanner
    scanner = SubsetScanner(trades_df, r_col="R")
    scanner.add_groupby("regime_at_entry")
    scanner.add_groupby("session")
    scanner.add_groupby("direction", map_fn=lambda d: "long" if d == 1 else "short")
    results = scanner.scan()
    scanner.print_report(results)

Usage as CLI:
    python -m scripts.subset_scan --csv logs/exit_profile_xauusd.csv --r-col net_r
    python -m scripts.subset_scan --csv logs/direction_split_xauusd.csv --r-col R \
        --groupby regime_at_entry --groupby session --groupby direction

The scanner produces:
  1. Per-subset metrics: n, mean_R, t_block, WR%, PF, sum_R
  2. Bonferroni-adjusted p-values (two-sided t-test H0: mean_R = 0)
  3. DSR (Deflated Sharpe Ratio) — probability true Sharpe > 0 after correcting
     for the number of subsets tested
  4. A verdict column: "sig" (survives correction), "weak" (suggestive), "noise"
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.metrics import block_bootstrap_t
from backtest.deflated_sharpe import (
    probabilistic_sharpe_ratio,
    deflated_sharpe_ratio,
    minimum_track_record_length,
)


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _t_to_p_two_sided(t_stat: float, df: int) -> float:
    """Two-sided p-value from t-statistic using the survival function."""
    from scipy import stats
    if df <= 0 or not np.isfinite(t_stat):
        return 1.0
    return float(2.0 * stats.t.sf(abs(t_stat), df))


def _bonferroni(p_values: list[float]) -> list[float]:
    """Bonferroni correction: p_adj = min(p * n_tests, 1.0)."""
    n = len(p_values)
    return [min(p * n, 1.0) for p in p_values]


def _dsr_correction(
    r_arrays: list[np.ndarray],
    n_total_subsets: int,
) -> list[float]:
    """DSR-based correction: probability true Sharpe > 0 for each subset,
    given that n_total_subsets were tested.

    Uses the deflated_sharpe_ratio from backtest.deflated_sharpe:
      DSR = P(Sharpe* > 0 | N trials, observed Sharpe, skew, kurtosis)

    Returns list of DSR values (higher = more significant).
    """
    dsr_values = []
    for r_arr in r_arrays:
        n = len(r_arr)
        if n < 2:
            dsr_values.append(0.0)
            continue
        try:
            result = deflated_sharpe_ratio(
                r_arr,
                n_trials=n_total_subsets,
            )
            dsr = result.get("dsr", 0.0)
            dsr_values.append(float(dsr) if np.isfinite(dsr) else 0.0)
        except Exception:
            dsr_values.append(0.0)
    return dsr_values


# ---------------------------------------------------------------------------
# Subset metrics
# ---------------------------------------------------------------------------

@dataclass
class SubsetResult:
    """Metrics for one subset of trades."""
    label: str
    n: int
    mean_R: float
    sum_R: float
    WR_pct: float
    PF: float
    t_block: float
    sharpe_est: float  # annualized-ish Sharpe proxy (mean/std * sqrt(n))
    p_value_raw: float
    p_value_bonf: float
    dsr: float
    verdict: str  # "sig" | "weak" | "noise" | "too_few"

    def to_dict(self) -> dict:
        return {
            "subset": self.label,
            "n": self.n,
            "mean_R": round(self.mean_R, 4),
            "sum_R": round(self.sum_R, 3),
            "WR%": round(self.WR_pct, 1),
            "PF": round(self.PF, 2),
            "t_block": round(self.t_block, 3),
            "sharpe_est": round(self.sharpe_est, 3),
            "p_raw": round(self.p_value_raw, 6),
            "p_bonf": round(self.p_value_bonf, 6),
            "DSR": round(self.dsr, 4),
            "verdict": self.verdict,
        }


def _compute_subset_metrics(
    r_values: np.ndarray,
    label: str,
    min_trades: int = 5,
) -> SubsetResult:
    """Compute metrics for a single subset of R-multiplicators."""
    n = len(r_values)
    if n == 0:
        return SubsetResult(label, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "too_few")

    mean_r = float(r_values.mean())
    sum_r = float(r_values.sum())
    wins = r_values[r_values > 0]
    losses = r_values[r_values <= 0]
    wr = 100.0 * len(wins) / n if n > 0 else 0.0
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)

    # Block-bootstrap t. The block size must adapt to the subset length:
    # with block ~= n-1 there is a single valid block start, every bootstrap
    # resample is identical, std -> 0 and t collapses to 0 — small subsets
    # would never reach significance. Cap the block at ~n/3 so small subsets
    # still get a spread of valid block starts.
    if n >= 2:
        block = min(20, max(2, n // 3))
        t_block = block_bootstrap_t(r_values.tolist(), block=block)
    else:
        t_block = 0.0
    t_block = float(np.clip(t_block, -50.0, 50.0)) if np.isfinite(t_block) else 0.0

    # Sharpe proxy: mean_R / std_R * sqrt(n)
    std_r = float(r_values.std(ddof=1)) if n >= 2 else 0.0
    if std_r > 1e-9 and np.isfinite(std_r):
        sharpe_est = mean_r / std_r * math.sqrt(n)
    elif abs(mean_r) > 1e-9:
        # Near-zero std with non-zero mean: very confident signal
        sharpe_est = np.sign(mean_r) * min(abs(mean_r) * math.sqrt(n) * 10, 50.0)
    else:
        sharpe_est = 0.0

    # Constant non-zero series: zero variance means the signal is
    # deterministic, so the t-statistic is effectively infinite in magnitude
    # (the block bootstrap collapses to std=0 and would return 0.0).
    if n >= 2 and std_r <= 1e-9 and abs(mean_r) > 1e-9:
        t_block = np.sign(mean_r) * 50.0

    # Raw p-value from block bootstrap t
    if n >= 2 and abs(t_block) > 0:
        df = max(n - 1, 1)
        p_raw = _t_to_p_two_sided(t_block, df)
    else:
        p_raw = 1.0

    if n < min_trades:
        verdict = "too_few"
    else:
        verdict = "pending"  # will be set after correction

    return SubsetResult(
        label=label,
        n=n,
        mean_R=mean_r,
        sum_R=sum_r,
        WR_pct=wr,
        PF=pf,
        t_block=t_block,
        sharpe_est=sharpe_est,
        p_value_raw=p_raw,
        p_value_bonf=1.0,  # placeholder
        dsr=0.0,  # placeholder
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class SubsetScanner:
    """Scan all subsets of a trades DataFrame by categorical groupby columns.

    Example:
        scanner = SubsetScanner(trades_df, r_col="R")
        scanner.add_groupby("regime_at_entry")
        scanner.add_groupby("session")
        scanner.add_groupby("direction", map_fn=lambda d: "long" if d == 1 else "short")
        results = scanner.scan()
    """

    def __init__(self, df: pd.DataFrame, r_col: str = "R", min_trades: int = 5):
        self.df = df.copy()
        self.r_col = r_col
        self.min_trades = min_trades
        self._groupby_cols: list[tuple[str, Callable | None]] = []
        self._custom_filters: list[tuple[str, Callable]] = []

    def add_groupby(self, col: str, map_fn: Callable | None = None):
        """Add a column to group by. Optional map_fn transforms values."""
        self._groupby_cols.append((col, map_fn))
        return self

    def add_filter(self, name: str, filter_fn: Callable[[pd.DataFrame], pd.Series]):
        """Add a named boolean filter (e.g. "direction=long" -> df.direction == 1)."""
        self._custom_filters.append((name, filter_fn))
        return self

    def _generate_subsets(self) -> list[tuple[str, pd.DataFrame]]:
        """Generate all subset DataFrames from groupby columns + custom filters."""
        subsets = []

        # Baseline: all trades
        subsets.append(("ALL", self.df))

        # Groupby combinations (power set of groupby columns)
        for r in range(1, len(self._groupby_cols) + 1):
            for combo in itertools.combinations(self._groupby_cols, r):
                col_names = [c[0] for c in combo]
                map_fns = {c[0]: c[1] for c in combo}

                # Get unique value combinations
                df_temp = self.df.copy()
                for col_name, map_fn in combo:
                    if map_fn:
                        df_temp[f"_scan_{col_name}"] = df_temp[col_name].apply(map_fn)
                        col_names_replaced = [f"_scan_{c}" if c == col_name else c for c in col_names]
                    else:
                        col_names_replaced = col_names

                # Use the possibly-mapped column names
                mapped_cols = []
                for col_name in col_names:
                    if map_fns.get(col_name):
                        mapped_cols.append(f"_scan_{col_name}")
                    else:
                        mapped_cols.append(col_name)

                for values, group_df in df_temp.groupby(mapped_cols):
                    if not isinstance(values, tuple):
                        values = (values,)
                    label_parts = []
                    for col_name, val in zip(col_names, values):
                        label_parts.append(f"{col_name}={val}")
                    label = " & ".join(label_parts)
                    subsets.append((label, group_df))

                # Clean up temp columns
                for col_name in col_names:
                    temp = f"_scan_{col_name}"
                    if temp in df_temp.columns:
                        del df_temp[temp]

        # Custom filters
        for name, filter_fn in self._custom_filters:
            mask = filter_fn(self.df)
            subsets.append((name, self.df[mask]))

        return subsets

    def scan(self) -> list[SubsetResult]:
        """Run the scan: compute metrics for all subsets, apply corrections."""
        subsets = self._generate_subsets()
        results = []
        r_arrays = []  # keep raw R arrays for DSR

        for label, sub_df in subsets:
            r_values = sub_df[self.r_col].dropna().values.astype(float)
            r_arrays.append(r_values)
            result = _compute_subset_metrics(r_values, label, self.min_trades)
            results.append(result)

        # Multiple testing corrections
        n_tests = len([r for r in results if r.verdict != "too_few"])
        if n_tests == 0:
            return results

        # Bonferroni
        p_values = [r.p_value_raw for r in results]
        p_bonf = _bonferroni(p_values)
        for r, pb in zip(results, p_bonf):
            r.p_value_bonf = pb

        # DSR correction (pass raw R arrays, not pre-computed Sharpe)
        dsr_values = _dsr_correction(r_arrays, n_tests)
        for r, dsr in zip(results, dsr_values):
            r.dsr = dsr

        # Verdicts. ``sig_neg`` marks subsets that are ROBUSTLY LOSING (the
        # two-sided p-value is significant and the mean is negative) — as
        # important to avoid as ``sig`` subsets are to keep. DSR<0.05 confirms
        # the true Sharpe is almost surely below zero given the trials tested.
        for r in results:
            if r.verdict == "too_few":
                continue
            if r.mean_R < 0 and r.p_value_bonf < 0.05 and r.dsr < 0.05:
                r.verdict = "sig_neg"
            elif r.p_value_bonf < 0.05 and r.dsr > 0.95:
                r.verdict = "sig"
            elif r.p_value_bonf < 0.25 or r.dsr > 0.80:
                r.verdict = "weak"
            else:
                r.verdict = "noise"

        # Sort: sig, sig_neg, weak, noise, too_few
        order = {"sig": 0, "sig_neg": 1, "weak": 2, "noise": 3, "too_few": 4}
        results.sort(key=lambda r: (order.get(r.verdict, 9), -r.n))

        return results

    def print_report(self, results: list[SubsetResult]):
        """Print a formatted report."""
        n_tests = len([r for r in results if r.verdict != "too_few"])
        n_sig = sum(1 for r in results if r.verdict == "sig")
        n_sig_neg = sum(1 for r in results if r.verdict == "sig_neg")
        n_weak = sum(1 for r in results if r.verdict == "weak")
        n_noise = sum(1 for r in results if r.verdict == "noise")

        print(f"\n{'='*72}")
        print(f"SUBSET SCAN: {len(results)} subsets tested, "
              f"{n_sig} significant(+), {n_sig_neg} significant(-), "
              f"{n_weak} weak, {n_noise} noise")
        print(f"Corrections: Bonferroni (n={n_tests}), DSR (N_trials={n_tests})")
        print(f"{'='*72}\n")

        # Header
        hdr = (f"{'verdict':>7} {'subset':<35} {'n':>5} {'meanR':>8} "
               f"{'sumR':>8} {'WR%':>6} {'PF':>5} {'t_blk':>6} "
               f"{'p_raw':>8} {'p_bonf':>8} {'DSR':>6}")
        print(hdr)
        print("-" * len(hdr))

        for r in results:
            if r.verdict == "too_few":
                vmark = "  ---"
            elif r.verdict == "sig":
                vmark = "  ***"
            elif r.verdict == "sig_neg":
                vmark = "  ###"
            elif r.verdict == "weak":
                vmark = "  ~~~"
            else:
                vmark = "    ."

            print(f"{vmark} {r.label:<35} {r.n:>5} {r.mean_R:>+8.4f} "
                  f"{r.sum_R:>+8.2f} {r.WR_pct:>5.1f}% {r.PF:>5.2f} "
                  f"{r.t_block:>+6.2f} {r.p_value_raw:>8.4f} "
                  f"{r.p_value_bonf:>8.4f} {r.dsr:>6.3f}")

        # Summary
        print(f"\n{'='*72}")
        print("SIGNIFICANT SUBSETS (survive Bonferroni + DSR):")
        sig = [r for r in results if r.verdict == "sig"]
        if sig:
            for r in sig:
                print(f"  {r.label}: n={r.n}, meanR={r.mean_R:+.4f}, "
                      f"sumR={r.sum_R:+.2f}, t={r.t_block:+.2f}, "
                      f"p_bonf={r.p_value_bonf:.4f}, DSR={r.dsr:.3f}")
        else:
            print("  (none)")

        print(f"\nSIGNIFICANTLY NEGATIVE SUBSETS (robust losers — avoid):")
        neg = [r for r in results if r.verdict == "sig_neg"]
        if neg:
            for r in neg:
                print(f"  {r.label}: n={r.n}, meanR={r.mean_R:+.4f}, "
                      f"sumR={r.sum_R:+.2f}, p_bonf={r.p_value_bonf:.4f}, "
                      f"DSR={r.dsr:.3f}")
        else:
            print("  (none)")

        print(f"\nWEAK SUBSETS (suggestive, need more data):")
        weak = [r for r in results if r.verdict == "weak"]
        if weak:
            for r in weak:
                print(f"  {r.label}: n={r.n}, meanR={r.mean_R:+.4f}, "
                      f"DSR={r.dsr:.3f}")
        else:
            print("  (none)")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Subset-scan with Bonferroni/DSR multiple-testing correction"
    )
    parser.add_argument("--csv", required=True, help="Path to trades CSV")
    parser.add_argument("--r-col", default="R", help="Column name for R-multiplicators")
    parser.add_argument("--groupby", action="append", default=[],
                        help="Column to group by (repeat for multiple)")
    parser.add_argument("--min-trades", type=int, default=5,
                        help="Minimum trades per subset (default: 5)")
    parser.add_argument("--out", default=None, help="Output CSV path")
    args = parser.parse_args(argv)

    df = pd.read_csv(args.csv)
    print(f"Loaded {len(df)} trades from {args.csv}")
    print(f"R column: {args.r_col}")

    scanner = SubsetScanner(df, r_col=args.r_col, min_trades=args.min_trades)

    for col in args.groupby:
        if col not in df.columns:
            print(f"WARNING: column '{col}' not in CSV, skipping")
            continue
        # Auto-detect direction mapping
        if col == "direction":
            scanner.add_groupby(col, map_fn=lambda d: "long" if d == 1 else "short")
        else:
            scanner.add_groupby(col)

    results = scanner.scan()
    scanner.print_report(results)

    if args.out:
        rows = [r.to_dict() for r in results]
        pd.DataFrame(rows).to_csv(args.out, index=False)
        print(f"CSV -> {args.out}")


if __name__ == "__main__":
    main()
