# -*- coding: utf-8 -*-
"""Backtest S/R proximity filter impact on scanner signals."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

from challenge.manual.scanner import scan_setup
from challenge.manual.sr_zones import detect_sr_zones, format_zones

CANDLES_DIR = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest\candles"
WATCHLIST = ["AAPL", "NVDA", "TSLA", "SPY", "GLD", "COIN", "AMD", "MU", "MRVL", "PLTR",
             "SHOP", "SMCI", "RDDT", "RKLB", "ABNB", "BA", "CAT", "KO", "MRK", "CSCO"]

DATES = [dt.date(2026, 7, 28), dt.date(2026, 7, 29), dt.date(2026, 7, 30),
         dt.date(2026, 7, 31), dt.date(2026, 8, 3), dt.date(2026, 8, 4),
         dt.date(2026, 8, 5), dt.date(2026, 8, 6), dt.date(2026, 8, 7),
         dt.date(2026, 8, 10), dt.date(2026, 8, 11), dt.date(2026, 8, 12),
         dt.date(2026, 8, 13), dt.date(2026, 8, 14), dt.date(2026, 8, 17),
         dt.date(2026, 8, 18), dt.date(2026, 8, 19)]


def load_candles(sym):
    p = os.path.join(CANDLES_DIR, sym + ".json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def run_scan(cfg, label):
    """Run scanner on all tickers/dates with given config."""
    results = []
    for d in DATES:
        for sym in WATCHLIST:
            candles = load_candles(sym)
            if not candles:
                continue
            res = scan_setup(sym, d, candles, dt.time(13, 30), cfg)
            results.append(res)
    return results


def classify_results(results):
    """Classify scan results into categories."""
    tradable = [r for r in results if r.tradable]
    no_go = [r for r in results if not r.tradable]
    sr_blocked = [r for r in no_go
                  if any("S/R proximity" in ng for ng in r.no_go)]
    other_blocked = [r for r in no_go if r not in sr_blocked]

    # Grade breakdown
    grade_a = [r for r in tradable if r.grade == "A"]
    grade_b = [r for r in tradable if r.grade == "B"]

    return {
        "total": len(results),
        "tradable": len(tradable),
        "blocked": len(no_go),
        "sr_blocked": len(sr_blocked),
        "other_blocked": len(other_blocked),
        "grade_a": len(grade_a),
        "grade_b": len(grade_b),
        "tradable_list": tradable,
        "sr_blocked_list": sr_blocked,
    }


def main():
    print("=" * 90)
    print("S/R PROXIMITY FILTER BACKTEST")
    print("=" * 90)
    print(f"Tickers: {len(WATCHLIST)}  |  Dates: {len(DATES)}  |  "
          f"Total combos: {len(WATCHLIST) * len(DATES)}\n")

    # === Run WITHOUT S/R filter ===
    cfg_off = {"target_rr": 3.5, "sr_proximity_buffer_usd": 0}
    results_off = run_scan(cfg_off, "no S/R")
    stats_off = classify_results(results_off)

    print("BASELINE (no S/R filter):")
    print(f"  Total scans:        {stats_off['total']}")
    print(f"  Tradable signals:   {stats_off['tradable']}  "
          f"(A={stats_off['grade_a']}, B={stats_off['grade_b']})")
    print(f"  Blocked:            {stats_off['blocked']}")
    print()

    # === Run WITH S/R filter at buffer=$2 (primary) ===
    cfg_2 = {"target_rr": 3.5, "sr_proximity_buffer_usd": 2.0}
    results_2 = run_scan(cfg_2, "S/R $2")
    stats_2 = classify_results(results_2)

    rejected = stats_off["tradable"] - stats_2["tradable"]
    reject_pct = 100 * rejected / stats_off["tradable"] if stats_off["tradable"] else 0

    print(f"S/R FILTER buffer=$2.0:")
    print(f"  Tradable signals:   {stats_2['tradable']}  "
          f"(A={stats_2['grade_a']}, B={stats_2['grade_b']})")
    print(f"  S/R rejected:       {rejected}  ({reject_pct:.0f}% of baseline)")
    print(f"  Retained:           {stats_2['tradable']}  "
          f"({100 - reject_pct:.0f}% of baseline)")
    print()

    # === Detailed analysis at buffer=2.0 ===
    cfg_2 = {"target_rr": 3.5, "sr_proximity_buffer_usd": 2.0}
    results_2 = run_scan(cfg_2, "S/R $2")
    stats_2 = classify_results(results_2)

    print("=" * 90)
    print("DETAILED: signals REJECTED by S/R filter (buffer=$2)")
    print("=" * 90)
    for r in stats_2["sr_blocked_list"][:20]:
        sr_reason = [ng for ng in r.no_go if "S/R proximity" in ng]
        print(f"  {r.date} {r.symbol:6s} {r.bias:6s} entry=${r.entry:.2f} "
              f"stop=${r.stop:.2f} target=${r.target:.2f}")
        for reason in sr_reason:
            print(f"    -> {reason}")
    if len(stats_2["sr_blocked_list"]) > 20:
        print(f"  ... and {len(stats_2['sr_blocked_list']) - 20} more")

    # === Signals that PASS with S/R filter ===
    print(f"\n{'=' * 90}")
    print("SIGNALS RETAINED by S/R filter (buffer=$2)")
    print("=" * 90)
    for r in stats_2["tradable_list"][:20]:
        zones = detect_sr_zones(load_candles(r.symbol),
                                dt.date.fromisoformat(r.date))
        nearest = sorted(zones, key=lambda z: abs(z.price - r.entry))
        nearest_str = f"${nearest[0].price:.2f} ({nearest[0].direction})" if nearest else "none"
        print(f"  {r.date} {r.symbol:6s} {r.grade} {r.bias:6s} "
              f"entry=${r.entry:.2f}  RR={r.rr:.1f}  nearest_zone={nearest_str}")
    if len(stats_2["tradable_list"]) > 20:
        print(f"  ... and {len(stats_2['tradable_list']) - 20} more")

    # === S/R zone quality analysis ===
    print(f"\n{'=' * 90}")
    print("S/R ZONE QUALITY ANALYSIS")
    print("=" * 90)
    # How many rejected signals were within $2 of a zone?
    total_zones_used = 0
    total_rejected = 0
    for r in stats_2["sr_blocked_list"]:
        zones = detect_sr_zones(load_candles(r.symbol),
                                dt.date.fromisoformat(r.date))
        if zones:
            nearest = min(zones, key=lambda z: abs(z.price - r.entry))
            dist = abs(nearest.price - r.entry)
            if dist <= 2.0:
                total_zones_used += 1
        total_rejected += 1

    print(f"  Signals rejected:  {total_rejected}")
    print(f"  Rejected near zone: {total_zones_used}  "
          f"({100 * total_zones_used / total_rejected:.0f}% if total_rejected > 0 else 0)")

    # === Summary ===
    print(f"\n{'=' * 90}")
    print("SUMMARY")
    print("=" * 90)
    print(f"  Baseline (no filter):   {stats_off['tradable']} tradable signals")
    print(f"  S/R filter $2.0:         {stats_2['tradable']} tradable  "
          f"({rejected} rejected, {reject_pct:.0f}%)")
    print(f"\n  Recommendation: buffer=$2.0 balances signal quality vs quantity")


if __name__ == "__main__":
    main()
