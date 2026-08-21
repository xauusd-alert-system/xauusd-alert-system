# -*- coding: utf-8 -*-
"""Sweep ORB filter parameters to find optimal volume/close thresholds."""
import json, os, datetime as dt
from collections import defaultdict
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


def build_days(candles):
    days = {}
    for c in candles:
        ts = c["time"]
        utc = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        if utc.weekday() >= 5:
            continue
        sec = utc.hour * 3600 + utc.minute * 60 + utc.second
        if not (OPEN_SEC <= sec <= CLOSE_SEC):
            continue
        days.setdefault(utc.date(), []).append(c)
    return days


def fee(price, qty):
    return max(1.0, 0.0004 * price * qty)


def _session_bucket(utc_sec):
    if utc_sec >= 18 * 3600 + 15 * 60:
        return "degraded"
    if utc_sec >= 14 * 3600:
        return "prime"
    return "normal"


def _avg_volume(vols, lookback=20):
    if len(vols) < 3:
        return 1.0
    recent = vols[-lookback:]
    if recent and isinstance(recent[0], dict):
        recent = [b.get("volume", 0) for b in recent]
    return sum(recent) / len(recent) if recent else 1.0


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
            failed = True
            break
        if day_pnl[d] <= -daily_stop:
            failed = True
            break
        if equity >= start + target and days_used >= min_days:
            passed = True
            break
    return {"equity_end": round(equity, 2), "passed": passed, "failed": failed, "days_used": days_used}


def run_orb(candles, min_vol_ratio=1.3, require_close=True,
            stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0, slip=SLIP,
            use_be=True, use_partial=True):
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < 35:
            continue
        range_end = bars[0]["time"] + 30 * 60
        rb = [b for b in bars if b["time"] < range_end]
        if not rb:
            continue
        rng_high = max(b["high"] for b in rb)
        rng_low = min(b["low"] for b in rb)
        if rng_high <= rng_low:
            continue
        open_pos = None
        took_trade = False
        vol_history = []
        for b in bars:
            t = b["time"]
            if t < range_end:
                vol_history.append(b.get("volume", 0))
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            bucket = _session_bucket(sec)
            vol = b.get("volume", 0)
            avg_vol = _avg_volume(vol_history) if vol_history else 1.0
            vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0

            if open_pos is None and not took_trade:
                if b["high"] >= rng_high:
                    if min_vol_ratio > 0 and len(vol_history) >= 3 and vol_ratio < min_vol_ratio:
                        took_trade = True
                        continue
                    if require_close and b["close"] <= rng_high:
                        continue
                    entry = (rng_high if b["open"] < rng_high else b["open"]) * (1 + slip)
                    qty = (risk_per_trade / stop_pct) / entry
                    risk_dist = entry * stop_pct
                    open_pos = {"side": 1, "entry": entry, "qty": qty,
                                "stop": entry * (1 - stop_pct),
                                "tp": entry + risk_dist * tp_ratio,
                                "entry_bar": t, "session_bucket": bucket,
                                "volume_ratio": vol_ratio, "risk_dist": risk_dist,
                                "be_moved": False, "partial_closed": False}
                    took_trade = True
                elif b["low"] <= rng_low:
                    if min_vol_ratio > 0 and len(vol_history) >= 3 and vol_ratio < min_vol_ratio:
                        took_trade = True
                        continue
                    if require_close and b["close"] >= rng_low:
                        continue
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - slip)
                    qty = (risk_per_trade / stop_pct) / entry
                    risk_dist = entry * stop_pct
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - risk_dist * tp_ratio,
                                "entry_bar": t, "session_bucket": bucket,
                                "volume_ratio": vol_ratio, "risk_dist": risk_dist,
                                "be_moved": False, "partial_closed": False}
                    took_trade = True

            if open_pos is None or t == open_pos["entry_bar"]:
                continue
            risk_dist = open_pos["risk_dist"]
            entry = open_pos["entry"]

            if use_be and not open_pos["be_moved"]:
                if open_pos["side"] == 1 and (b["high"] - entry) >= 0.5 * risk_dist:
                    open_pos["stop"] = entry
                    open_pos["be_moved"] = True
                elif open_pos["side"] == -1 and (entry - b["low"]) >= 0.5 * risk_dist:
                    open_pos["stop"] = entry
                    open_pos["be_moved"] = True

            if use_partial and not open_pos["partial_closed"]:
                if open_pos["side"] == 1 and (b["high"] - entry) >= risk_dist:
                    open_pos["partial_closed"] = True
                    open_pos["qty"] = max(0.01, open_pos["qty"] * 0.5)
                elif open_pos["side"] == -1 and (entry - b["low"]) >= risk_dist:
                    open_pos["partial_closed"] = True
                    open_pos["qty"] = max(0.01, open_pos["qty"] * 0.5)

            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, slip, "eod")
                trades.append(open_pos)
                open_pos = None
                break
            if open_pos["side"] == 1:
                if b["low"] * (1 - slip) <= open_pos["stop"]:
                    _close(open_pos, open_pos["stop"], t, slip, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + slip) >= open_pos["tp"]:
                    _close(open_pos, open_pos["tp"], t, slip, "target")
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + slip) >= open_pos["stop"]:
                    _close(open_pos, open_pos["stop"], t, slip, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - slip) <= open_pos["tp"]:
                    _close(open_pos, open_pos["tp"], t, slip, "target")
                    trades.append(open_pos)
                    open_pos = None

        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], slip, "eod")
            trades.append(open_pos)
    return trades


def row_stats(all_trades):
    n = len(all_trades)
    if n == 0:
        return "0 trades"
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
    if isinstance(result, str):
        print(f"  {label:<35s} {result}")
        return
    n, wr, pf, net, avg_r, fees, sim = result
    print(f"  {label:<35s} {n:>4d} trades  WR={wr:>4.0f}%  PF={pf:>5.2f}  "
          f"net=${net:>+7.0f}  avgR={avg_r:>6.3f}  fees=${fees:>5.0f}  "
          f"ch_pass={sim['passed']}  days={sim['days_used']}")


def main():
    candle_dir = os.path.join(BASE, "candles")
    all_files = sorted(f.replace(".json", "") for f in os.listdir(candle_dir) if f.endswith(".json"))
    print(f"Loading {len(all_files)} tickers...")
    all_candles = {}
    for t in all_files:
        all_candles[t] = load_candles(t)
    total_bars = sum(len(c) for c in all_candles.values())
    print(f"Total bars: {total_bars:,}\n")

    # === 1. BASELINE (no filters, no BE, no partial) ===
    print("=" * 100)
    print("1. BASELINE: no volume filter, no close confirm, no BE, no partial")
    print("=" * 100)
    all_trades = []
    for t, c in all_candles.items():
        all_trades += run_orb(c, min_vol_ratio=0, require_close=False,
                              use_be=False, use_partial=False)
    print_row("BASELINE (raw ORB)", row_stats(all_trades))

    # === 2. VOLUME THRESHOLD SWEEP ===
    print(f"\n{'=' * 100}")
    print("2. VOLUME THRESHOLD SWEEP (close=ON, BE=ON, partial=ON)")
    print("=" * 100)
    for vol in [0, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0]:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += run_orb(c, min_vol_ratio=vol, require_close=True,
                                  use_be=True, use_partial=True)
        print_row(f"vol>={vol:.1f}", row_stats(all_trades))

    # === 3. CLOSE CONFIRMATION SWEEP ===
    print(f"\n{'=' * 100}")
    print("3. CLOSE CONFIRMATION SWEEP (vol=1.3, BE=ON, partial=ON)")
    print("=" * 100)
    for close in [False, True]:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += run_orb(c, min_vol_ratio=1.3, require_close=close,
                                  use_be=True, use_partial=True)
        print_row(f"close={'ON' if close else 'OFF'}", row_stats(all_trades))

    # === 4. COMBINED vol x close ===
    print(f"\n{'=' * 100}")
    print("4. COMBINED SWEEP: vol_ratio x close_confirm (BE=ON, partial=ON)")
    print("=" * 100)
    for vol in [0, 1.0, 1.2, 1.3, 1.5]:
        for close in [False, True]:
            all_trades = []
            for t, c in all_candles.items():
                all_trades += run_orb(c, min_vol_ratio=vol, require_close=close,
                                      use_be=True, use_partial=True)
            print_row(f"vol={vol:.1f} close={'ON' if close else 'OFF'}", row_stats(all_trades))

    # === 5. MANAGEMENT SWEEP (BE + partial) ===
    print(f"\n{'=' * 100}")
    print("5. MANAGEMENT SWEEP: BE + partial (vol=1.3, close=ON)")
    print("=" * 100)
    for be in [False, True]:
        for partial in [False, True]:
            all_trades = []
            for t, c in all_candles.items():
                all_trades += run_orb(c, min_vol_ratio=1.3, require_close=True,
                                      use_be=be, use_partial=partial)
            print_row(f"BE={'ON' if be else 'OFF'} partial={'ON' if partial else 'OFF'}",
                      row_stats(all_trades))

    # === 6. BEST CONFIG: detailed challenge sim ===
    print(f"\n{'=' * 100}")
    print("6. BEST CONFIG: day-by-day challenge sim")
    print("=" * 100)
    # Run best combo (will be determined by the sweep above)
    all_trades = []
    for t, c in all_candles.items():
        all_trades += run_orb(c, min_vol_ratio=1.3, require_close=True,
                              use_be=True, use_partial=True)

    # Day-by-day breakdown
    day_pnl = {}
    for tr in all_trades:
        d = dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date()
        day_pnl.setdefault(d, 0.0)
        day_pnl[d] += tr["pnl"]

    equity = 1000.0
    print(f"\n  {'Date':<12s} {'Day P&L':>10s} {'Equity':>10s} {'#Trades':>8s}")
    print(f"  {'-'*45}")
    for d in sorted(day_pnl):
        equity += day_pnl[d]
        n_day = sum(1 for tr in all_trades
                    if dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date() == d)
        bar = "+" * min(20, int(max(0, day_pnl[d] * 2))) if day_pnl[d] > 0 else \
              "-" * min(20, int(max(0, -day_pnl[d] * 2)))
        print(f"  {str(d):<12s} ${day_pnl[d]:>+8.2f}  ${equity:>8.2f}  {n_day:>5d}  {bar}")

    print(f"\n  Final equity: ${equity:.2f}  (net P&L: ${equity - 1000:.2f})")


if __name__ == "__main__":
    main()
