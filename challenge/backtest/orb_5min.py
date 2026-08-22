# -*- coding: utf-8 -*-
"""ORB and opening drive on 5-min bars — cleaner signals, less noise."""
import json, os, datetime as dt
import sys as _sys
_sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"
OPEN_SEC = 13 * 3600 + 30 * 60
CLOSE_SEC = 19 * 3600 + 55 * 60
FLAT_SEC = 19 * 3600 + 50 * 60
SLIP = 0.0005


def load_candles(ticker):
    with open(os.path.join(BASE, "candles", ticker + ".json"), encoding="utf-8") as f:
        return json.load(f)


def resample_5min(candles_1m):
    """Resample 1-min candles to 5-min bars."""
    days = {}
    for c in candles_1m:
        utc = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc)
        if utc.weekday() >= 5:
            continue
        sec = utc.hour * 3600 + utc.minute * 60 + utc.second
        if not (OPEN_SEC <= sec <= CLOSE_SEC):
            continue
        # Round down to 5-min boundary
        bar_start = c["time"] - (c["time"] % 300)
        days.setdefault(utc.date(), {}).setdefault(bar_start, []).append(c)

    bars5 = []
    for date in sorted(days):
        for bar_ts in sorted(days[date]):
            bar_candles = days[date][bar_ts]
            bars5.append({
                "time": bar_ts,
                "open": bar_candles[0]["open"],
                "high": max(b["high"] for b in bar_candles),
                "low": min(b["low"] for b in bar_candles),
                "close": bar_candles[-1]["close"],
                "volume": sum(b.get("volume", 0) for b in bar_candles),
            })
    return bars5


def build_days_5min(bars5):
    days = {}
    for b in bars5:
        utc = dt.datetime.fromtimestamp(b["time"], dt.timezone.utc)
        days.setdefault(utc.date(), []).append(b)
    return days


def fee(price, qty):
    return max(1.0, 0.0004 * price * qty)


def _close(pos, price, ts, slip, reason):
    pos["exit_price"] = price * (1 - slip * pos["side"])
    pos["exit_ts"] = ts
    pos["exit_reason"] = reason
    pos["pnl"] = (pos["exit_price"] - pos["entry"]) * pos["side"] * pos["qty"] \
                 - fee(pos["entry"], pos["qty"]) - fee(pos["exit_price"], pos["qty"])


def simulate(all_trades, start=1000.0, daily_stop=25.0, total_stop=60.0,
             target=80.0, min_days=5):
    day_pnl = {}
    for tr in all_trades:
        d = dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date()
        day_pnl.setdefault(d, 0.0)
        day_pnl[d] += tr["pnl"]
    equity = start
    failed = passed = False
    days_used = 0
    for d in sorted(day_pnl):
        equity += day_pnl[d]
        days_used += 1
        if equity <= start - total_stop:
            failed = True; break
        if day_pnl[d] <= -daily_stop:
            failed = True; break
        if equity >= start + target and days_used >= min_days:
            passed = True; break
    return {"equity_end": round(equity, 2), "passed": passed, "failed": failed, "days_used": days_used}


def row_stats(all_trades):
    n = len(all_trades)
    if n == 0:
        return None
    wins = sum(1 for t in all_trades if t["pnl"] > 0)
    wr = wins / n * 100
    gw = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    gl = sum(t["pnl"] for t in all_trades if t["pnl"] <= 0)
    pf = gw / max(1e-9, abs(gl)) if gl else float("inf")
    net = gw + gl
    r_list = []
    for t in all_trades:
        risk = abs(t["entry"] - t["stop"]) * t["qty"]
        if risk > 0:
            r_list.append(t["pnl"] / risk)
    avg_r = sum(r_list) / len(r_list) if r_list else 0
    fees = sum(fee(t["entry"], t["qty"]) + fee(t["exit_price"], t["qty"]) for t in all_trades)
    sim = simulate(all_trades)
    return n, wr, pf, net, avg_r, fees, sim


def print_row(label, result):
    if result is None:
        print(f"  {label:<45s} 0 trades")
        return
    n, wr, pf, net, avg_r, fees, sim = result
    print(f"  {label:<45s} {n:>4d}  WR={wr:>4.0f}%  PF={pf:>5.2f}  "
          f"net=${net:>+7.0f}  avgR={avg_r:>6.3f}  fees=${fees:>5.0f}  "
          f"pass={sim['passed']}  days={sim['days_used']}")


def _run_orb(bars5, range_bars=6, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0):
    """ORB on 5-min bars. range_bars=6 = 30min, 12=60min."""
    days = build_days_5min(bars5)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda b: b["time"])
        if len(bars) < range_bars + 4:
            continue
        rng = bars[:range_bars]
        rng_high = max(b["high"] for b in rng)
        rng_low = min(b["low"] for b in rng)
        if rng_high <= rng_low:
            continue
        open_pos = None; took = False
        for b in bars[range_bars:]:
            t = b["time"]
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if open_pos is None and not took:
                if b["high"] >= rng_high:
                    entry = (rng_high if b["open"] < rng_high else b["open"]) * (1 + SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": 1, "entry": entry, "qty": qty,
                                "stop": entry * (1 - stop_pct),
                                "tp": entry + entry * stop_pct * tp_ratio, "bar": t}
                    took = True
                elif b["low"] <= rng_low:
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - entry * stop_pct * tp_ratio, "bar": t}
                    took = True
            if open_pos is None or t == open_pos["bar"]:
                continue
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos); open_pos = None; break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos); open_pos = None
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos); open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos); open_pos = None
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos); open_pos = None
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
            trades.append(open_pos)
    return trades


def _run_opening_drive(bars5, drive_bars=3, stop_pct=0.005, tp_ratio=1.5,
                       risk_per_trade=5.0, min_body_ratio=0.5):
    """Opening drive on 5-min bars. drive_bars=3 = first 15min, 6=30min."""
    days = build_days_5min(bars5)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda b: b["time"])
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
        open_pos = {"side": bias, "entry": entry, "qty": qty,
                    "stop": stop, "tp": tp, "bar": bars[drive_bars - 1]["time"]}
        for b in bars[drive_bars:]:
            t = b["time"]
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if open_pos is None:
                break
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos); open_pos = None; break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos); open_pos = None
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos); open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos); open_pos = None
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos); open_pos = None
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
            trades.append(open_pos)
    return trades


def main():
    candle_dir = os.path.join(BASE, "candles")
    tickers = sorted(f.replace(".json", "") for f in os.listdir(candle_dir) if f.endswith(".json"))
    print(f"Loading {len(tickers)} tickers, resampling to 5-min...")

    # Pre-compute 5-min bars for all tickers
    all_bars5 = {}
    total_5min_bars = 0
    for t in tickers:
        c = load_candles(t)
        b5 = resample_5min(c)
        all_bars5[t] = b5
        total_5min_bars += len(b5)
    print(f"Total 5-min bars: {total_5min_bars:,}\n")

    # === 1. Main comparison: 5-min vs 1-min ===
    print("=" * 110)
    print("5-MIN TIMEFRAME: ORB + OPENING DRIVE")
    print("=" * 110)
    print(f"  {'Strategy':<45s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s} {'fees$':>6s} {'pass':>5s}")
    print(f"  {'-'*100}")

    strategies = [
        # ORB variants on 5-min
        ("ORB 6-bar (30min) 0.5%/1.5R", lambda b: _run_orb(b, range_bars=6)),
        ("ORB 6-bar (30min) 0.8%/2.0R", lambda b: _run_orb(b, range_bars=6, stop_pct=0.008, tp_ratio=2.0)),
        ("ORB 12-bar (60min) 0.5%/1.5R", lambda b: _run_orb(b, range_bars=12)),
        ("ORB 12-bar (60min) 0.8%/2.0R", lambda b: _run_orb(b, range_bars=12, stop_pct=0.008, tp_ratio=2.0)),
        ("ORB 4-bar (20min) 0.5%/1.5R", lambda b: _run_orb(b, range_bars=4)),
        # Opening drive on 5-min
        ("Drive 1 bar (5min) body>0.5", lambda b: _run_opening_drive(b, drive_bars=1)),
        ("Drive 2 bar (10min) body>0.5", lambda b: _run_opening_drive(b, drive_bars=2)),
        ("Drive 3 bar (15min) body>0.5", lambda b: _run_opening_drive(b, drive_bars=3)),
        ("Drive 3 bar body>0.6", lambda b: _run_opening_drive(b, drive_bars=3, min_body_ratio=0.6)),
        ("Drive 3 bar body>0.7", lambda b: _run_opening_drive(b, drive_bars=3, min_body_ratio=0.7)),
        ("Drive 6 bar (30min) body>0.5", lambda b: _run_opening_drive(b, drive_bars=6)),
    ]

    all_results = {}
    for label, fn in strategies:
        all_trades = []
        for t, bars5 in all_bars5.items():
            all_trades += fn(bars5)
        r = row_stats(all_trades)
        print_row(label, r)
        all_results[label] = (all_trades, r)

    # === 2. ORB range sweep on 5-min ===
    print(f"\n{'=' * 110}")
    print("ORB RANGE SWEEP (5-min bars, stop=0.5%, tp=1.5R)")
    print("=" * 110)
    print(f"  {'range_bars':<12s} {'minutes':<10s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*55}")
    for rb in [2, 3, 4, 6, 8, 12, 16]:
        all_trades = []
        for t, bars5 in all_bars5.items():
            all_trades += _run_orb(bars5, range_bars=rb)
        r = row_stats(all_trades)
        if r is None:
            print(f"  {rb:>10d}  {rb*5:>7d}min   0 trades")
        else:
            n, wr, pf, net, avg_r, fees, sim = r
            print(f"  {rb:>10d}  {rb*5:>7d}min  {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}")

    # === 3. Stop/TP sweep on best ORB ===
    print(f"\n{'=' * 110}")
    print("ORB STOP/TP SWEEP (5-min, 6-bar range)")
    print("=" * 110)
    print(f"  {'stop%':<8s} {'tp_R':<6s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*50}")
    for sp, tr in [(0.003, 1.5), (0.005, 1.5), (0.005, 2.0), (0.005, 2.5),
                   (0.008, 1.5), (0.008, 2.0), (0.008, 2.5), (0.008, 3.0),
                   (0.01, 2.0), (0.01, 3.0)]:
        all_trades = []
        for t, bars5 in all_bars5.items():
            all_trades += _run_orb(bars5, range_bars=6, stop_pct=sp, tp_ratio=tr)
        r = row_stats(all_trades)
        if r is None:
            print(f"  {sp*100:.1f}%   {tr:.1f}R   0 trades")
        else:
            n, wr, pf, net, avg_r, fees, sim = r
            print(f"  {sp*100:.1f}%   {tr:.1f}R  {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}")

    # === 4. Per-ticker for best 5-min strategy ===
    print(f"\n{'=' * 110}")
    print("PER-TICKER: ORB 6-bar (30min) 0.5%/1.5R on 5-min bars")
    print("=" * 110)
    print(f"  {'Ticker':<10s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*50}")
    for t in tickers:
        trades = _run_orb(all_bars5[t], range_bars=6)
        if not trades:
            continue
        n, wr, pf, net, avg_r, fees, sim = row_stats(trades)
        marker = " ***" if net > 0 else ""
        print(f"  {t:<10s} {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}{marker}")

    # === 5. Day-by-day for best ===
    print(f"\n{'=' * 110}")
    print("DAY-BY-DAY: Best 5-min strategy")
    print("=" * 110)
    # Find best from strategies
    best_label = max(all_results, key=lambda k: all_results[k][1][3] if all_results[k][1] else -9999)
    best_trades = all_results[best_label][0]
    print(f"  Strategy: {best_label}\n")
    day_pnl = {}
    for tr in best_trades:
        d = dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date()
        day_pnl.setdefault(d, 0.0)
        day_pnl[d] += tr["pnl"]
    equity = 1000.0
    for d in sorted(day_pnl):
        equity += day_pnl[d]
        bar = "+" * min(20, int(max(0, day_pnl[d] * 2))) if day_pnl[d] > 0 else \
              "-" * min(20, int(max(0, -day_pnl[d] * 2)))
        print(f"  {d}  ${day_pnl[d]:>+7.2f}  ${equity:>8.2f}  {bar}")
    sim = simulate(best_trades)
    print(f"\n  Final: equity=${sim['equity_end']:.0f}  pass={sim['passed']}  days={sim['days_used']}")


if __name__ == "__main__":
    main()
