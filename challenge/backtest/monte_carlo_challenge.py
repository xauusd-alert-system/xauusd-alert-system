# -*- coding: utf-8 -*-
"""Monte Carlo simulation of the prop challenge with pre-trade constraints.

Uses real R-distribution from backtest data + checklist rules:
- Max 3 trades/day, stop after 2 losses
- Daily stop -$25, total stop -$60
- Target +$80
- Commission-aware sizing
"""

import datetime as dt
import json
import os
import random
import statistics
import sys as _sys

_sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"
OPEN_SEC = 13 * 3600 + 30 * 60
CLOSE_SEC = 19 * 3600 + 55 * 60
FLAT_SEC = 19 * 3600 + 50 * 60
SLIP = 0.0005
STARTING_EQUITY = 1000.0
DAILY_STOP = 25.0
TOTAL_STOP = 60.0
TARGET = 80.0
RISK_PER_TRADE = 5.0
MAX_TRADES_PER_DAY = 3
MAX_LOSSES_PER_DAY = 2
NUM_SIMS = 20000


def load_candles(ticker):
    with open(os.path.join(BASE, "candles", ticker + ".json"), encoding="utf-8") as f:
        return json.load(f)


def build_days(candles):
    days = {}
    for c in candles:
        utc = dt.datetime.fromtimestamp(c["time"], dt.UTC)
        if utc.weekday() >= 5:
            continue
        sec = utc.hour * 3600 + utc.minute * 60 + utc.second
        if not (OPEN_SEC <= sec <= CLOSE_SEC):
            continue
        days.setdefault(utc.date(), []).append(c)
    return days


def fee(price, qty):
    return max(1.0, 0.0004 * price * qty)


def _close(pos, price, ts, slip, reason):
    pos["exit_price"] = price * (1 - slip * pos["side"])
    pos["exit_ts"] = ts
    pos["exit_reason"] = reason
    pos["pnl"] = (
        (pos["exit_price"] - pos["entry"]) * pos["side"] * pos["qty"]
        - fee(pos["entry"], pos["qty"])
        - fee(pos["exit_price"], pos["qty"])
    )


def run_opening_drive_all(candles, drive_bars=3, stop_pct=0.005, tp_ratio=3.5, risk_per_trade=5.0, min_body_ratio=0.5):
    """Run opening drive on all days, return list of trade results (pnl in $)."""
    days = build_days(candles)
    day_trades = {}
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < drive_bars + 4:
            continue
        drive = bars[:drive_bars]
        d_open = drive[0]["open"]
        d_close = drive[-1]["close"]
        d_high = max(b["high"] for b in drive)
        d_low = min(b["low"] for b in drive)
        d_range = d_high - d_low
        d_body = abs(d_close - d_open)
        if d_range <= 0 or d_body < min_body_ratio * d_range:
            continue
        if d_close > d_open:
            bias = 1
            entry = d_close * (1 + SLIP)
            stop = d_low * (1 - SLIP)
        else:
            bias = -1
            entry = d_close * (1 - SLIP)
            stop = d_high * (1 + SLIP)
        risk_dist = abs(entry - stop)
        if risk_dist <= 0:
            continue
        tp = entry + bias * risk_dist * tp_ratio
        qty = (risk_per_trade / stop_pct) / entry
        open_pos = {
            "side": bias,
            "entry": entry,
            "qty": qty,
            "stop": stop,
            "tp": tp,
            "bar": bars[drive_bars - 1]["time"],
        }
        for b in bars[drive_bars:]:
            t = b["time"]
            utc = dt.datetime.fromtimestamp(t, dt.UTC)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if open_pos is None:
                break
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    open_pos = None
                    break
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    open_pos = None
                    break
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    open_pos = None
                    break
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    open_pos = None
                    break
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
        if open_pos is not None:
            day_trades.setdefault(str(date), []).append(open_pos["pnl"])
    return day_trades


def collect_r_distribution(all_day_trades):
    """Extract R-multiples from actual trade P&L."""
    r_values = []
    for date, trades in all_day_trades.items():
        for pnl in trades:
            r = pnl / RISK_PER_TRADE  # normalize to R
            r_values.append(r)
    return r_values


def simulate_challenge(r_values, n_sims=NUM_SIMS):
    """Monte Carlo: simulate challenge outcome n_sims times.

    Each simulation picks random days from the r_distribution pool,
    applies checklist rules (3 trades/day, 2 losses stop-day),
    and checks if target is reached before total stop.
    """
    random.seed(42)
    results = []

    for _ in range(n_sims):
        equity = STARTING_EQUITY
        day_start = STARTING_EQUITY
        total_start = STARTING_EQUITY
        trading_days = 0
        days_used = 0
        passed = False
        failed = False
        day_trades = 0
        day_losses = 0
        flattened_today = False

        # Simulate up to 60 trading days
        for day in range(60):
            days_used += 1
            trading_days += 1
            day_pnl = 0.0
            day_trades = 0
            day_losses = 0
            flattened_today = False

            # Each day: take up to MAX_TRADES_PER_DAY trades from the pool
            for trade in range(MAX_TRADES_PER_DAY):
                # Pick a random R from the distribution
                r = random.choice(r_values)
                pnl = r * RISK_PER_TRADE

                # Commission: ~$2 round-trip per share
                commission = 2.0
                pnl -= commission

                day_pnl += pnl
                day_trades += 1

                if pnl < 0:
                    day_losses += 1

                # Checklist: stop-day after 2 losses
                if day_losses >= MAX_LOSSES_PER_DAY:
                    flattened_today = True
                    break

            equity += day_pnl

            # Daily stop check (equity-based)
            daily_pnl = equity - day_start
            if daily_pnl <= -DAILY_STOP:
                failed = True
                break

            # Total stop check
            total_pnl = equity - total_start
            if total_pnl <= -TOTAL_STOP:
                failed = True
                break

            # Target check (minimum 5 trading days)
            if equity >= STARTING_EQUITY + TARGET and trading_days >= 5:
                passed = True
                break

            # Reset for next day
            day_start = equity

        results.append(
            {
                "passed": passed,
                "failed": failed,
                "equity_end": equity,
                "days_used": days_used,
                "trading_days": trading_days,
            }
        )

    return results


def main():
    # Load all candle data
    candle_dir = os.path.join(BASE, "candles")
    tickers = sorted(f.replace(".json", "") for f in os.listdir(candle_dir) if f.endswith(".json"))
    print(f"Loading {len(tickers)} tickers...")

    all_day_trades = {}
    for t in tickers:
        candles = load_candles(t)
        day_trades = run_opening_drive_all(candles)
        for d, trades in day_trades.items():
            all_day_trades.setdefault(d, []).extend(trades)

    # Collect R distribution
    r_values = collect_r_distribution(all_day_trades)
    print(f"R-distribution: {len(r_values)} trades from {len(all_day_trades)} days")
    print(f"  Mean R: {statistics.mean(r_values):+.3f}")
    print(f"  Median R: {statistics.median(r_values):+.3f}")
    print(f"  Std R: {statistics.stdev(r_values):.3f}")
    print(f"  Win rate: {100 * sum(1 for r in r_values if r > 0) / len(r_values):.0f}%")
    print(f"  Avg win: {statistics.mean([r for r in r_values if r > 0]):+.3f}R")
    print(f"  Avg loss: {statistics.mean([r for r in r_values if r <= 0]):+.3f}R")
    print(f"  Profit factor: {sum(r for r in r_values if r > 0) / abs(sum(r for r in r_values if r <= 0)):.2f}")

    # Run Monte Carlo
    print(f"\nRunning {NUM_SIMS:,} Monte Carlo simulations...")
    results = simulate_challenge(r_values)

    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if r["failed"]]
    neither = [r for r in results if not r["passed"] and not r["failed"]]

    p_pass = 100 * len(passed) / len(results)
    p_fail = 100 * len(failed) / len(results)

    print(f"\n{'=' * 70}")
    print(f"MONTE CARLO RESULTS ({NUM_SIMS:,} simulations)")
    print(f"{'=' * 70}")
    print(f"  P(+$80 target):    {p_pass:.1f}%  ({len(passed):,}/{NUM_SIMS:,})")
    print(f"  P(-$60 stop):      {p_fail:.1f}%  ({len(failed):,}/{NUM_SIMS:,})")
    print(f"  Neither (timeout): {100 - p_pass - p_fail:.1f}%  ({len(neither):,})")

    # Days to target (for passed sims)
    if passed:
        days_to_target = [r["days_used"] for r in passed]
        print("\n  Days to target (passed only):")
        print(f"    Median:   {statistics.median(days_to_target):.0f} days")
        print(f"    Mean:     {statistics.mean(days_to_target):.1f} days")
        print(f"    P25:      {sorted(days_to_target)[len(days_to_target) // 4]:.0f} days")
        print(f"    P75:      {sorted(days_to_target)[3 * len(days_to_target) // 4]:.0f} days")
        print(f"    Min:      {min(days_to_target):.0f} days")
        print(f"    Max:      {max(days_to_target):.0f} days")

    # Equity distribution
    all_equity = [r["equity_end"] for r in results]
    print("\n  Final equity distribution:")
    print(f"    Median:   ${statistics.median(all_equity):.0f}")
    print(f"    Mean:     ${statistics.mean(all_equity):.0f}")
    print(f"    P10:      ${sorted(all_equity)[len(all_equity) // 10]:.0f}")
    print(f"    P25:      ${sorted(all_equity)[len(all_equity) // 4]:.0f}")
    print(f"    P75:      ${sorted(all_equity)[3 * len(all_equity) // 4]:.0f}")
    print(f"    P90:      ${sorted(all_equity)[9 * len(all_equity) // 10]:.0f}")

    # Risk of ruin
    ruin = sum(1 for r in results if r["equity_end"] <= STARTING_EQUITY - TOTAL_STOP)
    print(f"\n  Risk of ruin (equity <= ${STARTING_EQUITY - TOTAL_STOP:.0f}): {100 * ruin / len(results):.1f}%")

    # Equity percentile curves
    print("\n  Equity percentiles by day:")
    max_days = max(r["days_used"] for r in results)
    for day in range(1, min(max_days + 1, 31)):
        day_equities = []
        for r in results:
            if r["days_used"] >= day:
                # Approximate equity at this day
                day_equities.append(r["equity_end"])
        if day_equities:
            p10 = sorted(day_equities)[len(day_equities) // 10]
            p50 = sorted(day_equities)[len(day_equities) // 2]
            p90 = sorted(day_equities)[9 * len(day_equities) // 10]
            print(f"    Day {day:>2d}: P10=${p10:>7.0f}  P50=${p50:>7.0f}  P90=${p90:>7.0f}")

    # Sensitivity: what win rate is needed for P(pass) > 70%?
    print(f"\n{'=' * 70}")
    print("SENSITIVITY: required win rate for P(+$80) > 70%")
    print("=" * 70)
    for wr_target in [40, 45, 50, 55, 60]:
        # Create synthetic R distribution with target win
        # rate
        wins = [r for r in r_values if r > 0]
        losses = [r for r in r_values if r <= 0]
        avg_win = statistics.mean(wins) if wins else 0.5
        avg_loss = statistics.mean(losses) if losses else -0.5
        synthetic_r = [avg_win] * int(wr_target * 10) + [avg_loss] * int((100 - wr_target) * 10)
        res = simulate_challenge(synthetic_r, n_sims=5000)
        p = 100 * sum(1 for r in res if r["passed"]) / len(res)
        med_days = (
            statistics.median([r["days_used"] for r in res if r["passed"]]) if any(r["passed"] for r in res) else "n/a"
        )
        print(f"  WR={wr_target}%: P(pass)={p:.0f}%  median_days={med_days}")


if __name__ == "__main__":
    main()
