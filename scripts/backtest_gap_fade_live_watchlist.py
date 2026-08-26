"""Backtest gap_fade on the live watchlist to check if it was ever profitable.

Compares gap_fade performance on:
1. Original backtest watchlist (AAPL, NVDA, TSLA, etc.)
2. Live watchlist (CAN, CIFR, CLSK, MARA, MSTR, etc.)

Uses the same candle data and simulation logic as calibrate_quality.py.
"""
import json, os, sys, datetime as dt
from pathlib import Path

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)

from challenge.manual.scanner import scan_gap_fade, bars_of_day, SESSION_START_UTC
from challenge.manual.quality_score import compute_quality_score

CANDLE_DIR = Path(ROOT) / "data" / "backtest" / "candles"

# Load config
import yaml
with open(Path(ROOT) / "challenge" / "manual" / "manual_config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}

# Watchlists
ORIGINAL_WATCHLIST = {"AAPL", "ABNB", "AMD", "BA", "CAT", "COIN", "CSCO", "GLD",
                      "KO", "MRK", "MRVL", "MU", "NVDA", "PLTR", "RDDT", "RKLB",
                      "SHOP", "SMCI", "SPY", "TSLA"}
LIVE_WATCHLIST = set(CFG.get("watchlist", []))

SESSION_START = dt.time(13, 30)
SESSION_END = dt.time(19, 55)


def sim_outcome(setup, candles_1m, date):
    """Simulate what happens after a gap_fade setup."""
    if not setup.tradable or setup.bias == "none":
        return "no_signal", 0.0, 0

    entry = setup.entry
    stop = setup.stop
    target = setup.target
    bias = setup.bias

    # Signal time: session open + 1 min (gap_fade fires at open)
    sess_start = dt.datetime.combine(date, SESSION_START, tzinfo=dt.timezone.utc).timestamp()
    signal_ts = sess_start + 60

    future = sorted([c for c in candles_1m if c["time"] > signal_ts], key=lambda x: x["time"])
    if not future:
        return "eod", 0.0, 0

    for c in future:
        utc = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc)
        t = utc.timetz().replace(tzinfo=None)
        if t >= SESSION_END:
            if bias == "long":
                r = (c["close"] - entry) / abs(entry - stop)
            else:
                r = (entry - c["close"]) / abs(stop - entry)
            mins = (c["time"] - signal_ts) / 60
            return "eod", round(r, 3), round(mins)

        if bias == "long":
            if c["low"] <= stop:
                return "stop", -1.0, round((c["time"] - signal_ts) / 60)
            if c["high"] >= target:
                rr = abs(target - entry) / abs(entry - stop)
                return "target", round(rr, 3), round((c["time"] - signal_ts) / 60)
        else:
            if c["high"] >= stop:
                return "stop", -1.0, round((c["time"] - signal_ts) / 60)
            if c["low"] <= target:
                rr = abs(entry - target) / abs(stop - entry)
                return "target", round(rr, 3), round((c["time"] - signal_ts) / 60)

    if future:
        last = future[-1]
        if bias == "long":
            r = (last["close"] - entry) / abs(entry - stop)
        else:
            r = (entry - last["close"]) / abs(stop - entry)
        return "eod", round(r, 3), round((last["time"] - signal_ts) / 60)
    return "eod", 0.0, 0


def main():
    # Load candle data
    symbol_files = sorted(CANDLE_DIR.glob("*.json"))
    print(f"Loading {len(symbol_files)} symbol files...")

    all_days = set()
    symbol_data = {}
    for sf in symbol_files:
        data = json.loads(sf.read_text(encoding="utf-8"))
        symbol_data[sf.stem] = data
        for c in data:
            d = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc).date()
            all_days.add(d)

    trading_days = sorted(all_days)
    print(f"Trading days: {trading_days[0]} to {trading_days[-1]} ({len(trading_days)} days)")

    # Run gap_fade backtest on both watchlists
    results_original = []
    results_live = []

    for day in trading_days:
        if day.weekday() >= 5:
            continue
        for sym, candles in symbol_data.items():
            day_candles = bars_of_day(candles, day)
            if len(day_candles) < 40:
                continue

            gap = scan_gap_fade(sym, day, candles, session_start_utc=SESSION_START, cfg=CFG)
            if not gap.tradable:
                continue

            outcome, r, mins = sim_outcome(gap, candles, day)
            if outcome == "no_signal":
                continue

            row = {"date": str(day), "symbol": sym, "bias": gap.bias,
                   "entry": gap.entry, "stop": gap.stop, "target": gap.target,
                   "rr": gap.rr, "outcome": outcome, "r": r, "minutes": mins}

            if sym in ORIGINAL_WATCHLIST:
                results_original.append(row)
            if sym in LIVE_WATCHLIST:
                results_live.append(row)

    # Print results
    def print_stats(label, results):
        if not results:
            print(f"\n{label}: NO setups found")
            return
        wins = sum(1 for r in results if r["r"] > 0)
        losses = sum(1 for r in results if r["r"] < 0)
        avg_r = sum(r["r"] for r in results) / len(results)
        sum_r = sum(r["r"] for r in results)
        print(f"\n{'='*60}")
        print(f"{label}: {len(results)} setups")
        print(f"  WR: {wins}/{len(results)} = {wins/len(results):.1%}")
        print(f"  avgR: {avg_r:+.3f}")
        print(f"  sumR: {sum_r:+.2f}")

        # By symbol
        by_sym = {}
        for r in results:
            by_sym.setdefault(r["symbol"], []).append(r)
        print(f"\n  By symbol:")
        for sym in sorted(by_sym.keys()):
            sr = by_sym[sym]
            sw = sum(1 for r in sr if r["r"] > 0)
            sa = sum(r["r"] for r in sr) / len(sr)
            ss = sum(r["r"] for r in sr)
            print(f"    {sym:8s}: {len(sr):2d} trades, WR {sw/len(sr):.0%}, avgR {sa:+.3f}, sumR {ss:+.2f}")

    print_stats("ORIGINAL WATCHLIST (big-cap)", results_original)
    print_stats("LIVE WATCHLIST (small-cap crypto)", results_live)

    # Compare
    if results_original and results_live:
        orig_avg = sum(r["r"] for r in results_original) / len(results_original)
        live_avg = sum(r["r"] for r in results_live) / len(results_live)
        print(f"\n{'='*60}")
        print(f"COMPARISON:")
        print(f"  Original: {len(results_original)} trades, avgR {orig_avg:+.3f}")
        print(f"  Live:     {len(results_live)} trades, avgR {live_avg:+.3f}")
        print(f"  Delta:    {live_avg - orig_avg:+.3f} avgR")


if __name__ == "__main__":
    main()
