# -*- coding: utf-8 -*-
"""Monte Carlo: quality-filtered vs unfiltered challenge simulation.

Uses real R-distributions from quality_calibration.json (386 trades, 20 symbols x 25 days).
Compares P(+$80), median days, and risk metrics for filtered and unfiltered.
"""
import json, os, random, statistics, math
from pathlib import Path

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
CALIBRATION = Path(ROOT) / "data" / "backtest" / "quality_calibration.json"

# Challenge parameters (Stage 1, Hash Hedge)
STARTING_EQUITY = 1000.0
TARGET_PROFIT = 80.0        # +$80
TOTAL_STOP = 100.0           # -$100
DAILY_STOP = 50.0            # -$50
RISK_PER_TRADE = 5.0         # $5
MAX_TRADES_PER_DAY = 3
MAX_LOSSES_PER_DAY = 2
COMMISSION_PER_TRADE = 2.0   # ~$2 round-trip
NUM_SIMS = 10000


def load_r_distributions():
    """Load R-values from calibration data, split into filtered/unfiltered."""
    data = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    results = data["detailed_results"]
    
    # Extract quality thresholds
    thresholds = {
        stype: rec["min_quality_score"]
        for stype, rec in data["recommendation"].items()
    }
    
    all_r = []       # unfiltered: all tradable setups
    filtered_r = []  # filtered: only above-threshold
    
    by_type_all = {}     # unfiltered by type
    by_type_filt = {}    # filtered by type
    
    for row in results:
        r_val = row["r"]
        q_score = row.get("quality_score", 0)
        stype = row["type"]
        
        # All
        all_r.append(r_val)
        by_type_all.setdefault(stype, []).append(r_val)
        
        # Filtered
        threshold = thresholds.get(stype, 0)
        if q_score >= threshold:
            filtered_r.append(r_val)
            by_type_filt.setdefault(stype, []).append(r_val)
    
    return all_r, filtered_r, by_type_all, by_type_filt, thresholds


def stats(arr, label=""):
    """Print distribution stats."""
    if not arr:
        print(f"  {label}: NO DATA")
        return
    n = len(arr)
    wr = 100 * sum(1 for r in arr if r > 0) / n
    mu = statistics.mean(arr)
    med = statistics.median(arr)
    sig = statistics.stdev(arr) if n > 1 else 0
    wins = [r for r in arr if r > 0]
    losses = [r for r in arr if r <= 0]
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = statistics.mean(losses) if losses else 0
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    print(f"  {label}: n={n}  WR={wr:.1f}%  mean={mu:+.3f}R  median={med:+.3f}R  "
          f"std={sig:.3f}  avgW={avg_win:+.3f}  avgL={avg_loss:+.3f}  PF={pf:.2f}")


def simulate(r_values, n_sims=NUM_SIMS, seed=42):
    """Monte Carlo challenge simulation.
    
    Each day: draw up to 3 trades from R-distribution.
    Stop-day after 2 losses.
    Daily stop -$50, total stop -$100.
    Target +$80 (minimum 5 trading days).
    No time limit (cap at 200 days).
    """
    random.seed(seed)
    results = []
    
    for _ in range(n_sims):
        equity = STARTING_EQUITY
        trading_days = 0
        passed = False
        failed = False
        reason = ""
        
        for day in range(200):  # cap at 200 days
            day_start = equity
            trading_days += 1
            day_losses = 0
            day_trades = 0
            
            for _ in range(MAX_TRADES_PER_DAY):
                r = random.choice(r_values)
                pnl = r * RISK_PER_TRADE - COMMISSION_PER_TRADE
                equity += pnl
                day_trades += 1
                
                if pnl < 0:
                    day_losses += 1
                
                # Stop-day: 2 losses
                if day_losses >= MAX_LOSSES_PER_DAY:
                    break
            
            # Daily stop check (equity-based)
            daily_pnl = equity - day_start
            if daily_pnl <= -DAILY_STOP:
                failed = True
                reason = "daily_stop"
                break
            
            # Total stop check
            total_pnl = equity - STARTING_EQUITY
            if total_pnl <= -TOTAL_STOP:
                failed = True
                reason = "total_stop"
                break
            
            # Target check (min 5 days)
            if equity >= STARTING_EQUITY + TARGET_PROFIT and trading_days >= 5:
                passed = True
                reason = "target"
                break
        
        results.append({
            "passed": passed,
            "failed": failed,
            "reason": reason,
            "equity_end": round(equity, 2),
            "trading_days": trading_days,
        })
    
    return results


def analyze(results, label=""):
    """Print analysis of simulation results."""
    n = len(results)
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if r["failed"]]
    neither = [r for r in results if not r["passed"] and not r["failed"]]
    
    p_pass = 100 * len(passed) / n
    p_fail = 100 * len(failed) / n
    p_neither = 100 * len(neither) / n
    
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Simulations:     {n:,}")
    print(f"  P(+$80 target):  {p_pass:.1f}%  ({len(passed):,})")
    print(f"  P(-$100 stop):   {p_fail:.1f}%  ({len(failed):,})")
    print(f"  P(neither):      {p_neither:.1f}%  ({len(neither):,})")
    
    if passed:
        days = [r["trading_days"] for r in passed]
        days_sorted = sorted(days)
        print(f"\n  Days to target (passed only):")
        print(f"    Median:   {statistics.median(days):.0f}d")
        print(f"    Mean:     {statistics.mean(days):.1f}d")
        print(f"    P10:      {days_sorted[len(days_sorted)//10]:.0f}d")
        print(f"    P25:      {days_sorted[len(days_sorted)//4]:.0f}d")
        print(f"    P75:      {days_sorted[3*len(days_sorted)//4]:.0f}d")
        print(f"    P90:      {days_sorted[9*len(days_sorted)//10]:.0f}d")
        print(f"    Min/Max:  {min(days)}d / {max(days)}d")
    
    # Equity distribution
    all_eq = [r["equity_end"] for r in results]
    all_eq_sorted = sorted(all_eq)
    print(f"\n  Final equity:")
    print(f"    Median:   ${statistics.median(all_eq):.0f}")
    print(f"    Mean:     ${statistics.mean(all_eq):.0f}")
    print(f"    P10:      ${all_eq_sorted[len(all_eq_sorted)//10]:.0f}")
    print(f"    P25:      ${all_eq_sorted[len(all_eq_sorted)//4]:.0f}")
    print(f"    P75:      ${all_eq_sorted[3*len(all_eq_sorted)//4]:.0f}")
    print(f"    P90:      ${all_eq_sorted[9*len(all_eq_sorted)//10]:.0f}")
    
    # Risk of ruin
    ruin = sum(1 for r in results if r["equity_end"] <= STARTING_EQUITY - TOTAL_STOP)
    print(f"\n  Risk of ruin: {100*ruin/n:.1f}%")
    
    return {
        "p_pass": p_pass,
        "p_fail": p_fail,
        "median_days": statistics.median([r["trading_days"] for r in passed]) if passed else None,
        "mean_equity": statistics.mean(all_eq),
        "median_equity": statistics.median(all_eq),
    }


def main():
    print("=" * 60)
    print("MONTE CARLO: Quality-Filtered vs Unfiltered Challenge Simulation")
    print("=" * 60)
    print(f"  Risk/trade: ${RISK_PER_TRADE}  |  Target: +${TARGET_PROFIT}")
    print(f"  Daily stop: -${DAILY_STOP}  |  Total stop: -${TOTAL_STOP}")
    print(f"  Max 3 trades/day, stop-day after 2 losses")
    print(f"  Commission: ~${COMMISSION_PER_TRADE}/trade\n")
    
    # Load distributions
    all_r, filtered_r, by_type_all, by_type_filt, thresholds = load_r_distributions()
    
    # Print stats
    print("--- R-Distribution Stats ---")
    stats(all_r, "UNFILTERED (all 3 scanners)")
    stats(filtered_r, "FILTERED (quality gate applied)")
    print()
    for stype in ["impulse", "gap_fade", "opening_drive"]:
        if stype in by_type_all:
            n_all = len(by_type_all[stype])
            n_filt = len(by_type_filt.get(stype, []))
            threshold = thresholds.get(stype, 0)
            print(f"  {stype}: {n_all} total -> {n_filt} pass Q>={threshold} "
                  f"({100*n_filt/max(1,n_all):.0f}%)")
    
    # Trade frequency estimate
    n_days = 25  # from calibration
    trades_per_day_all = len(all_r) / n_days
    trades_per_day_filt = len(filtered_r) / n_days
    print(f"\n  Trade frequency: unfiltered={trades_per_day_all:.1f}/day, "
          f"filtered={trades_per_day_filt:.1f}/day")
    
    # Run simulations
    print(f"\n--- Running {NUM_SIMS:,} simulations ---")
    
    print("\n[1/2] Unfiltered...")
    results_all = simulate(all_r, n_sims=NUM_SIMS)
    metrics_all = analyze(results_all, "UNFILTERED")
    
    print("\n[2/2] Filtered (quality gate)...")
    results_filt = simulate(filtered_r, n_sims=NUM_SIMS)
    metrics_filt = analyze(results_filt, "FILTERED")
    
    # Comparison summary
    print(f"\n{'='*60}")
    print("COMPARISON SUMMARY")
    print(f"{'='*60}")
    print(f"{'Metric':<30s} {'Unfiltered':>12s} {'Filtered':>12s} {'Diff':>12s}")
    print("-" * 66)
    
    rows = [
        ("P(+$80)", f"{metrics_all['p_pass']:.1f}%", f"{metrics_filt['p_pass']:.1f}%",
         f"{metrics_filt['p_pass'] - metrics_all['p_pass']:+.1f}%"),
        ("P(-$100)", f"{metrics_all['p_fail']:.1f}%", f"{metrics_filt['p_fail']:.1f}%",
         f"{metrics_filt['p_fail'] - metrics_all['p_fail']:+.1f}%"),
    ]
    # Add median days row conditionally
    if metrics_all['median_days'] and metrics_filt['median_days']:
        rows.append(("Median days to target", f"{metrics_all['median_days']:.0f}d",
                     f"{metrics_filt['median_days']:.0f}d",
                     f"{metrics_filt['median_days'] - metrics_all['median_days']:+.0f}d"))
    rows += [
        ("Mean final equity", f"${metrics_all['mean_equity']:.0f}",
         f"${metrics_filt['mean_equity']:.0f}",
         f"${metrics_filt['mean_equity'] - metrics_all['mean_equity']:+.0f}"),
        ("Median final equity", f"${metrics_all['median_equity']:.0f}",
         f"${metrics_filt['median_equity']:.0f}",
         f"${metrics_filt['median_equity'] - metrics_all['median_equity']:+.0f}"),
    ]
    for name, v1, v2, delta in rows:
        print(f"{name:<30s} {v1:>12s} {v2:>12s} {delta:>12s}")
    
    # Equity curves
    print(f"\n{'='*60}")
    print("EQUITY PERCENTILES BY DAY")
    print(f"{'='*60}")
    for label, results in [("UNFILTERED", results_all), ("FILTERED", results_filt)]:
        print(f"\n  {label}:")
        # Build equity curve data
        max_days = max(r["trading_days"] for r in results)
        for day in [1, 3, 5, 7, 10, 12, 15, 18, 20]:
            day_eq = [r["equity_end"] for r in results if r["trading_days"] >= day]
            if day_eq:
                p10 = sorted(day_eq)[len(day_eq)//10]
                p50 = sorted(day_eq)[len(day_eq)//2]
                p90 = sorted(day_eq)[9*len(day_eq)//10]
                print(f"    Day {day:>2d}: P10=${p10:>7.0f}  P50=${p50:>7.0f}  P90=${p90:>7.0f}")

    # Per-setup-type breakdown
    print(f"\n{'='*60}")
    print("PER-SETUP-TYPE SIMULATION (filtered only)")
    print(f"{'='*60}")
    for stype in ["impulse", "gap_fade", "opening_drive"]:
        r_vals = by_type_filt.get(stype, [])
        if len(r_vals) < 3:
            print(f"\n  {stype}: insufficient data ({len(r_vals)} trades)")
            continue
        res = simulate(r_vals, n_sims=5000)
        passed = [r for r in res if r["passed"]]
        p = 100 * len(passed) / len(res)
        days = [r["trading_days"] for r in passed]
        med = statistics.median(days) if days else None
        avg_eq = statistics.mean([r["equity_end"] for r in res])
        print(f"  {stype:20s} (n={len(r_vals):3d}): P(pass)={p:.1f}%  "
              f"median={med:.0f}d  avg_eq=${avg_eq:.0f}" if med else
              f"  {stype:20s} (n={len(r_vals):3d}): P(pass)={p:.1f}%  avg_eq=${avg_eq:.0f}")

    # Sensitivity: win rate needed for 70%+ pass
    print(f"\n{'='*60}")
    print("SENSITIVITY: required win rate for P(+$80) > 70%")
    print(f"{'='*60}")
    # Use filtered distribution as baseline
    wins = [r for r in filtered_r if r > 0]
    losses = [r for r in filtered_r if r <= 0]
    avg_win = statistics.mean(wins) if wins else 0.5
    avg_loss = statistics.mean(losses) if losses else -0.5
    
    for wr_target in [35, 40, 45, 50, 55, 60]:
        # Synthetic: mix of avg_win and avg_loss
        n_win = int(wr_target)
        n_loss = int(100 - wr_target)
        synth = [avg_win] * n_win + [avg_loss] * n_loss
        # Add some noise
        synth = [r + random.gauss(0, 0.1) for r in synth]
        res = simulate(synth, n_sims=5000, seed=hash(wr_target))
        passed = [r for r in res if r["passed"]]
        p = 100 * len(passed) / len(res)
        days = [r["trading_days"] for r in passed]
        med = statistics.median(days) if days else 0
        marker = " ?" if p >= 70 else ""
        print(f"  WR={wr_target}%: P(pass)={p:.0f}%  median={med:.0f}d{marker}")


if __name__ == "__main__":
    main()