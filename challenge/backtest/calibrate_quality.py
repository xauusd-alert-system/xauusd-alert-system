# -*- coding: utf-8 -*-
"""Calibrate quality filters across all 3 setup types on historical candle data.

For each symbol×day (20 symbols, ~17 trading days in July 28 – Aug 20),
run all 3 scanners, simulate outcomes, and compute quality scores.
Then find the optimal quality threshold per setup type.
"""
import json, os, sys, datetime as dt
from collections import defaultdict
from pathlib import Path

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)

from challenge.manual.scanner import (
    scan_setup, scan_gap_fade, scan_opening_drive,
    bars_of_day,
)
from challenge.manual.quality_score import compute_quality_score

CANDLE_DIR = Path(ROOT) / "data" / "backtest" / "candles"
OUT_PATH = Path(ROOT) / "data" / "backtest" / "quality_calibration.json"

# Config from manual_config.yaml
import yaml
with open(Path(ROOT) / "challenge" / "manual" / "manual_config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}

# Session model — from manual_config.yaml (same source as the alerter), not
# hardcoded: a config change propagates here automatically.
SESSION_START = dt.time(*map(int, CFG.get("session_start_utc", "13:30").split(":")))
SESSION_END = dt.time(*map(int, CFG.get("session_end_utc", "19:55").split(":")))

TARGET_RR = float(CFG.get("target_rr", 3.5))


def sim_outcome(setup, candles_1m, date):
    """Simulate what happens after a setup: iterate through 1-min candles
    to find stop-hit, target-hit, or EOD close. Returns (outcome, r, minutes)."""
    if not setup.tradable or setup.bias == "none":
        return "no_signal", 0.0, 0

    entry = setup.entry
    stop = setup.stop
    target = setup.target
    bias = setup.bias

    # Find signal time (from signal_bar, or from impulse, or from session open)
    signal_ts = None
    if setup.signal_bar:
        signal_ts = setup.signal_bar["time"]
    elif setup.impulse_bar:
        signal_ts = setup.impulse_bar["time"]
    else:
        # Use session open + 1 min
        sess_start = dt.datetime.combine(
            date, SESSION_START, tzinfo=dt.timezone.utc
        ).timestamp()
        signal_ts = sess_start + 60

    # Sort candles after signal
    future = sorted(
        [c for c in candles_1m if c["time"] > signal_ts],
        key=lambda x: x["time"]
    )
    if not future:
        return "eod", 0.0, 0

    for c in future:
        # Check if we've exceeded session end
        utc = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc)
        t = utc.timetz().replace(tzinfo=None)
        if t >= SESSION_END:
            # EOD close at last observed price
            if bias == "long":
                r = (c["close"] - entry) / abs(entry - stop)
            else:
                r = (entry - c["close"]) / abs(stop - entry)
            mins = (c["time"] - signal_ts) / 60
            return "eod", round(r, 3), round(mins)

        if bias == "long":
            if c["low"] <= stop:
                r = (stop - entry) / abs(entry - stop)
                mins = (c["time"] - signal_ts) / 60
                return "stop", round(r, 3), round(mins)
            if c["high"] >= target:
                r = (target - entry) / abs(entry - stop)
                mins = (c["time"] - signal_ts) / 60
                return "target", round(r, 3), round(mins)
        else:  # short
            if c["high"] >= stop:
                r = (entry - stop) / abs(stop - entry)
                mins = (c["time"] - signal_ts) / 60
                return "stop", round(r, 3), round(mins)
            if c["low"] <= target:
                r = (entry - target) / abs(stop - entry)
                mins = (c["time"] - signal_ts) / 60
                return "target", round(r, 3), round(mins)

    # Ran out of candles before EOD - treat as EOD
    if future:
        last = future[-1]
        if bias == "long":
            r = (last["close"] - entry) / abs(entry - stop)
        else:
            r = (entry - last["close"]) / abs(stop - entry)
        mins = (last["time"] - signal_ts) / 60
        return "eod", round(r, 3), round(mins)
    return "eod", 0.0, 0


def compute_quality_for_setup(setup, setup_type, candles_1m, date):
    """Compute quality score for a setup, with setup-specific enhancements."""
    signal_ts = None
    if setup.signal_bar:
        signal_ts = setup.signal_bar["time"]
    elif setup.impulse_bar:
        signal_ts = setup.impulse_bar["time"]
    else:
        sess_start = dt.datetime.combine(
            date, SESSION_START, tzinfo=dt.timezone.utc
        ).timestamp()
        signal_ts = sess_start + 60

    # Compute volume ratio for the signal bar
    vol_ratio = 1.0
    if setup.signal_bar and setup.signal_bar.get("volume", 0) > 0:
        day = bars_of_day(candles_1m, date)
        vols = [c.get("volume", 0) for c in day if c.get("volume", 0) > 0]
        if vols:
            avg_vol = sum(vols[-20:]) / min(20, len(vols))
            if avg_vol > 0:
                vol_ratio = setup.signal_bar["volume"] / avg_vol

    # Regime from setup
    regime = setup.trend15 if setup.trend15 else ""

    score = compute_quality_score(
        signal_ts=int(signal_ts),
        volume_ratio=vol_ratio,
        regime=regime,
        bias=setup.bias,
    )
    
    # Add setup-specific bonus/malus
    if setup_type == "gap_fade":
        # Bigger gap = higher conviction fade
        gap_size = abs(setup.entry - setup.target) / setup.target if setup.target else 0
        if gap_size > 0.02:
            score["total"] = min(100, score["total"] + 10)
        elif gap_size < 0.008:
            score["total"] = max(0, score["total"] - 10)
    elif setup_type == "opening_drive":
        # Stronger drive body = higher conviction
        if setup.signal_bar:
            body = abs(setup.signal_bar["close"] - setup.signal_bar["open"])
            rng = setup.signal_bar["high"] - setup.signal_bar["low"]
            if rng > 0 and body / rng > 0.7:
                score["total"] = min(100, score["total"] + 10)
    elif setup_type == "impulse":
        # Already has grade in the setup itself
        pass

    # Recompute grade
    if score["total"] >= 80:
        score["grade"] = "A"
    elif score["total"] >= 60:
        score["grade"] = "B"
    elif score["total"] >= 40:
        score["grade"] = "C"
    else:
        score["grade"] = "D"

    return score


def main():
    symbol_files = sorted(CANDLE_DIR.glob("*.json"))
    print(f"Loading {len(symbol_files)} symbol files...")

    all_days = set()
    symbol_data = {}
    for sf in symbol_files:
        data = json.loads(sf.read_text())
        symbol_data[sf.stem] = data
        for c in data:
            d = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc).date()
            all_days.add(d)

    trading_days = sorted(all_days)
    print(f"Trading days: {trading_days[0]} to {trading_days[-1]} ({len(trading_days)} days)")

    # Collect all setups with simulated outcomes and quality scores
    results = []
    total_scanned = 0

    for day in trading_days:
        # Skip weekends
        if day.weekday() >= 5:
            continue
        
        day_str = str(day)
        for sym, candles in symbol_data.items():
            day_candles = bars_of_day(candles, day)
            if len(day_candles) < 40:
                continue
            total_scanned += 1

            # 1. Impulse+pullback
            imp = scan_setup(sym, day, candles, session_start_utc=SESSION_START, cfg=CFG)
            if imp.tradable:
                outcome, r, mins = sim_outcome(imp, candles, day)
                quality = compute_quality_for_setup(imp, "impulse", candles, day)
                results.append({
                    "date": day_str, "symbol": sym, "type": "impulse",
                    "grade": imp.grade, "bias": imp.bias,
                    "quality_score": quality["total"],
                    "quality_grade": quality["grade"],
                    "outcome": outcome, "r": r, "minutes": mins,
                })

            # 2. Gap fade
            gap = scan_gap_fade(sym, day, candles, session_start_utc=SESSION_START, cfg=CFG)
            if gap.tradable:
                outcome, r, mins = sim_outcome(gap, candles, day)
                quality = compute_quality_for_setup(gap, "gap_fade", candles, day)
                results.append({
                    "date": day_str, "symbol": sym, "type": "gap_fade",
                    "grade": gap.grade, "bias": gap.bias,
                    "quality_score": quality["total"],
                    "quality_grade": quality["grade"],
                    "outcome": outcome, "r": r, "minutes": mins,
                })

            # 3. Opening drive
            od = scan_opening_drive(sym, day, candles, session_start_utc=SESSION_START, cfg=CFG)
            if od.tradable:
                outcome, r, mins = sim_outcome(od, candles, day)
                quality = compute_quality_for_setup(od, "opening_drive", candles, day)
                results.append({
                    "date": day_str, "symbol": sym, "type": "opening_drive",
                    "grade": od.grade, "bias": od.bias,
                    "quality_score": quality["total"],
                    "quality_grade": quality["grade"],
                    "outcome": outcome, "r": r, "minutes": mins,
                })

    print(f"Scanned {total_scanned} symbol×days, found {len(results)} tradable setups")
    
    # --- Analysis per setup type ---
    for setup_type in ["impulse", "gap_fade", "opening_drive"]:
        typed = [r for r in results if r["type"] == setup_type]
        if not typed:
            print(f"\n{'='*60}")
            print(f"  {setup_type}: NO setups found")
            continue
        
        print(f"\n{'='*60}")
        print(f"  {setup_type}: {len(typed)} setups")
        
        # Overall
        wins = sum(1 for r in typed if r["r"] > 0)
        losses = sum(1 for r in typed if r["r"] < 0)
        flats = sum(1 for r in typed if r["r"] == 0)
        avg_r = sum(r["r"] for r in typed) / len(typed)
        sum_r = sum(r["r"] for r in typed)
        print(f"  Overall: WR={wins}/{len(typed)}={wins/max(1,len(typed)):.1%}, "
              f"avgR={avg_r:+.3f}, sumR={sum_r:+.2f}")
        
        # By quality grade
        for grade in ["A", "B", "C", "D"]:
            graded = [r for r in typed if r["quality_grade"] == grade]
            if not graded:
                continue
            gwins = sum(1 for r in graded if r["r"] > 0)
            gavg = sum(r["r"] for r in graded) / len(graded)
            gsum = sum(r["r"] for r in graded)
            print(f"    Grade {grade} ({len(graded):3d}): WR={gwins}/{len(graded)}={gwins/len(graded):.1%}, "
                  f"avgR={gavg:+.3f}, sumR={gsum:+.2f}")
        
        # By quality score bracket
        brackets = [(0, 40, "0-40 (D)"), (40, 60, "40-60 (C)"), 
                     (60, 80, "60-80 (B)"), (80, 101, "80-100 (A)")]
        for lo, hi, label in brackets:
            bracketed = [r for r in typed if lo <= r["quality_score"] < hi]
            if not bracketed:
                continue
            bwins = sum(1 for r in bracketed if r["r"] > 0)
            bavg = sum(r["r"] for r in bracketed) / len(bracketed)
            bsum = sum(r["r"] for r in bracketed)
            print(f"    Score {label:>12s} ({len(bracketed):3d}): WR={bwins}/{len(bracketed)}={bwins/len(bracketed):.1%}, "
                  f"avgR={bavg:+.3f}, sumR={bsum:+.2f}")

        # Find optimal threshold
        print(f"\n  Threshold sweep (min quality score):")
        best_threshold = 0
        best_avg_r = -999
        for threshold in range(0, 101, 5):
            filtered = [r for r in typed if r["quality_score"] >= threshold]
            if len(filtered) < 3:
                continue
            favg = sum(r["r"] for r in filtered) / len(filtered)
            fwr = sum(1 for r in filtered if r["r"] > 0) / len(filtered)
            if favg > best_avg_r:
                best_avg_r = favg
                best_threshold = threshold
            marker = " [BEST]" if threshold == best_threshold else ""
            print(f"    min_score >= {threshold:3d}: {len(filtered):3d} trades, "
                  f"WR={fwr:.1%}, avgR={favg:+.3f}{marker}")

        print(f"\n  >> BEST: min_score >= {best_threshold}, gives avgR={best_avg_r:+.3f} "
              f"with {len([r for r in typed if r['quality_score'] >= best_threshold])} trades")

    # Save results
    summary = {
        "total_setups": len(results),
        "by_type": {},
        "recommendation": {},
    }
    for setup_type in ["impulse", "gap_fade", "opening_drive"]:
        typed = [r for r in results if r["type"] == setup_type]
        if not typed:
            continue
        # Find optimal
        best_t, best_avg = 0, -999
        for t in range(0, 101, 5):
            f = [r for r in typed if r["quality_score"] >= t]
            if len(f) < 3:
                continue
            favg = sum(r["r"] for r in f) / len(f)
            if favg > best_avg:
                best_avg, best_t = favg, t
        
        summary["by_type"][setup_type] = {
            "count": len(typed),
            "avg_r": round(sum(r["r"] for r in typed) / len(typed), 3),
            "total_r": round(sum(r["r"] for r in typed), 2),
        }
        summary["recommendation"][setup_type] = {
            "min_quality_score": best_t,
            "avg_r_at_threshold": round(best_avg, 3),
            "trades_at_threshold": len([r for r in typed if r["quality_score"] >= best_t]),
        }
    
    summary["detailed_results"] = results

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\nSaved to {OUT_PATH}")


if __name__ == "__main__":
    main()