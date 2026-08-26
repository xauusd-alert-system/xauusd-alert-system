# -*- coding: utf-8 -*-
"""Analyze intraday correlation between crypto_beta stocks on a specific date.

Fetches 1-min candles from UTEX and computes:
1. Intraday price correlation matrix
2. Opening drive direction alignment
3. Cross-stock stop-out clustering analysis
"""
from __future__ import annotations

import json
import os
import sys
import time
import datetime as dt
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from challenge.manual.alerter import refresh_access, fetch_candles, SYMBOLS

CFG_PATH = os.path.join(ROOT, "challenge", "manual", "manual_config.yaml")

import yaml
CFG = yaml.safe_load(open(CFG_PATH, encoding="utf-8"))

CLUSTER = "crypto_beta"
CLUSTER_MEMBERS = CFG.get("clusters", {}).get(CLUSTER, [])
# Session windows from manual_config.yaml (same source as the alerter), not
# hardcoded — a config change propagates here automatically.
SESSION_START = dt.time(*map(int, CFG.get("session_start_utc", "13:30").split(":")))
SESSION_END = dt.time(*map(int, CFG.get("session_end_utc", "19:55").split(":")))


def analyze_date(access, date_str: str):
    """Fetch candles for all cluster members and analyze correlation."""
    date = dt.date.fromisoformat(date_str)
    sess_start_ts = int(dt.datetime.combine(date, SESSION_START, tzinfo=dt.timezone.utc).timestamp())
    sess_end_ts = int(dt.datetime.combine(date, SESSION_END, tzinfo=dt.timezone.utc).timestamp())

    # Fetch candles for all cluster members + non-cluster for comparison
    all_syms = list(SYMBOLS.keys())
    candle_data = {}
    for sym in all_syms:
        sid = SYMBOLS.get(sym)
        if not sid:
            continue
        try:
            candles = fetch_candles(access, sid, candles_count=1500)
            # Filter to session day
            day_candles = [c for c in candles if sess_start_ts <= c["time"] <= sess_end_ts]
            if day_candles:
                candle_data[sym] = day_candles
                print(f"  {sym}: {len(day_candles)} candles", file=sys.stderr)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})", file=sys.stderr)

    print(f"\nFetched {len(candle_data)} symbols", file=sys.stderr)

    # --- Opening drive analysis ---
    print(f"\n{'='*70}")
    print(f"OPENING DRIVE ANALYSIS: {date_str}")
    print(f"{'='*70}")

    drive_minutes = int(CFG.get("opening_drive_minutes", 5))
    results = []

    for sym, candles in sorted(candle_data.items()):
        if len(candles) < drive_minutes + 5:
            continue
        drive_bars = candles[:drive_minutes]
        drive_open = drive_bars[0]["open"]
        drive_close = drive_bars[-1]["close"]
        drive_high = max(b["high"] for b in drive_bars)
        drive_low = min(b["low"] for b in drive_bars)
        drive_range = drive_high - drive_low
        drive_body = abs(drive_close - drive_open)

        if drive_range <= 0:
            continue

        body_ratio = drive_body / drive_range
        bias = "long" if drive_close > drive_open else "short"

        # Full session return
        session_close = candles[-1]["close"]
        session_return_pct = (session_close - candles[0]["open"]) / candles[0]["open"] * 100

        # Max adverse excursion (how far against the drive direction)
        if bias == "short":
            max_adverse = max(b["high"] for b in candles[drive_minutes:]) - drive_close
        else:
            max_adverse = drive_close - min(b["low"] for b in candles[drive_minutes:])
        max_adverse_pct = max_adverse / drive_close * 100

        is_cluster = sym in CLUSTER_MEMBERS
        results.append({
            "symbol": sym,
            "cluster": is_cluster,
            "bias": bias,
            "body_ratio": body_ratio,
            "session_return_pct": session_return_pct,
            "max_adverse_pct": max_adverse_pct,
            "drive_range_pct": drive_range / drive_close * 100,
        })

    # Print results grouped by cluster
    cluster_results = [r for r in results if r["cluster"]]
    non_cluster = [r for r in results if not r["cluster"]]

    print(f"\n{'Symbol':8s} {'Cluster':8s} {'Bias':6s} {'Body%':6s} {'Session%':9s} {'MaxAdv%':8s} {'Drive%':7s}")
    print("-" * 60)

    for r in sorted(results, key=lambda x: (-x["cluster"], x["symbol"])):
        cl = "CRYPTO" if r["cluster"] else ""
        print(f"{r['symbol']:8s} {cl:8s} {r['bias']:6s} {r['body_ratio']:5.0%} "
              f"{r['session_return_pct']:+8.2f}% {r['max_adverse_pct']:7.2f}% "
              f"{r['drive_range_pct']:6.2f}%")

    # --- Direction alignment ---
    if cluster_results:
        short_count = sum(1 for r in cluster_results if r["bias"] == "short")
        long_count = len(cluster_results) - short_count
        print(f"\nCLUSTER DIRECTION ALIGNMENT:")
        print(f"  Short: {short_count}/{len(cluster_results)} ({100*short_count/len(cluster_results):.0f}%)")
        print(f"  Long:  {long_count}/{len(cluster_results)} ({100*long_count/len(cluster_results):.0f}%)")

    # --- Correlation of session returns ---
    if len(results) >= 2:
        print(f"\nSESSION RETURN CORRELATION:")
        # Group returns by symbol
        returns = {r["symbol"]: r["session_return_pct"] for r in results}

        # Intraday correlation (minute-by-minute returns)
        print(f"\n  Minute-by-minute return correlation (Pearson):")
        # Build return series
        syms_with_data = [r["symbol"] for r in results]
        min_ts = sorted(set(t for s in syms_with_data for t in [c["time"] for c in candle_data[s]]))

        # Simple: correlate session returns across the full sample
        cluster_rets = [returns[s] for s in syms_with_data if s in CLUSTER_MEMBERS]
        non_cluster_rets = [returns[s] for s in syms_with_data if s not in CLUSTER_MEMBERS]

        if cluster_rets:
            avg_cluster = sum(cluster_rets) / len(cluster_rets)
            print(f"  Cluster avg return: {avg_cluster:+.2f}%")
        if non_cluster_rets:
            avg_non = sum(non_cluster_rets) / len(non_cluster_rets)
            print(f"  Non-cluster avg return: {avg_non:+.2f}%")

    # --- Pairwise correlation (close price series) ---
    print(f"\nPAIRWISE CLOSE-PRICE CORRELATION:")
    # Normalize each symbol's close series to % change from open
    norm_series = {}
    for sym in syms_with_data:
        candles = candle_data[sym]
        open_price = candles[0]["open"]
        if open_price > 0:
            norm_series[sym] = [(c["close"] - open_price) / open_price * 100 for c in candles]

    # Pearson correlation
    def pearson(a, b):
        n = min(len(a), len(b))
        if n < 3:
            return 0.0
        a, b = a[:n], b[:n]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
        std_a = (sum((x - mean_a) ** 2 for x in a) / n) ** 0.5
        std_b = (sum((x - mean_b) ** 2 for x in b) / n) ** 0.5
        if std_a == 0 or std_b == 0:
            return 0.0
        return cov / (std_a * std_b)

    # Compute correlation matrix for cluster members
    cluster_syms = [s for s in CLUSTER_MEMBERS if s in norm_series]
    if len(cluster_syms) >= 2:
        print(f"\n  Cluster intra-correlation ({len(cluster_syms)} symbols):")
        corrs = []
        for i in range(len(cluster_syms)):
            for j in range(i + 1, len(cluster_syms)):
                c = pearson(norm_series[cluster_syms[i]], norm_series[cluster_syms[j]])
                corrs.append(c)
        if corrs:
            avg_corr = sum(corrs) / len(corrs)
            min_corr = min(corrs)
            max_corr = max(corrs)
            print(f"    Average: {avg_corr:.3f}")
            print(f"    Range: {min_corr:.3f} to {max_corr:.3f}")
            print(f"    Pairs: {len(corrs)}")

            # Top 5 most correlated pairs
            pair_corrs = []
            for i in range(len(cluster_syms)):
                for j in range(i + 1, len(cluster_syms)):
                    c = pearson(norm_series[cluster_syms[i]], norm_series[cluster_syms[j]])
                    pair_corrs.append((cluster_syms[i], cluster_syms[j], c))
            pair_corrs.sort(key=lambda x: -x[2])
            print(f"\n  Top 5 correlated pairs:")
            for s1, s2, c in pair_corrs[:5]:
                print(f"    {s1:8s} - {s2:8s}: {c:.3f}")

    # Cross-correlation: cluster vs non-cluster
    non_cluster_syms = [s for s in norm_series if s not in CLUSTER_MEMBERS]
    if cluster_syms and non_cluster_syms:
        cross_corrs = []
        for cs in cluster_syms:
            for nc in non_cluster_syms:
                c = pearson(norm_series[cs], norm_series[nc])
                cross_corrs.append((cs, nc, c))
        if cross_corrs:
            avg_cross = sum(c[2] for c in cross_corrs) / len(cross_corrs)
            print(f"\n  Cluster vs Non-cluster avg correlation: {avg_cross:.3f}")

    # --- Stop-out clustering analysis ---
    print(f"\n{'='*70}")
    print(f"STOP-OUT CLUSTERING ANALYSIS")
    print(f"{'='*70}")

    # From outcomes_resolved.json
    outcomes_path = os.path.join(ROOT, "data", "manual", "outcomes_resolved.json")
    if os.path.exists(outcomes_path):
        with open(outcomes_path) as f:
            resolved = json.load(f)

        day_outcomes = {k: v for k, v in resolved.items() if k.startswith(date_str)}
        stops = {k: v for k, v in day_outcomes.items() if v.get("outcome") == "stop"}
        targets = {k: v for k, v in day_outcomes.items() if v.get("outcome") == "target"}

        cluster_stops = [k for k in stops if any(m in k for m in CLUSTER_MEMBERS)]
        non_cluster_stops = [k for k in stops if not any(m in k for m in CLUSTER_MEMBERS)]

        print(f"\n  Total stops: {len(stops)}")
        print(f"  Cluster stops: {len(cluster_stops)}")
        print(f"  Non-cluster stops: {len(non_cluster_stops)}")

        # Resolution time analysis
        print(f"\n  Resolution times (minutes from signal):")
        for k in sorted(stops.keys()):
            parts = k.split(":")
            sym = parts[-1]
            # Get from setup_outcomes.csv
            print(f"    {sym:8s}: {stops[k].get('resolved_utc', '?')}")


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-24"
    print(f"Fetching access token...", file=sys.stderr)
    try:
        access = refresh_access()
    except Exception as e:
        print(f"Cannot refresh token: {e}", file=sys.stderr)
        print("Analysis requires valid UTEX session. Token has expired.")
        print("Please re-login to the UTEX web terminal to refresh the token.")
        return 1

    analyze_date(access, date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
