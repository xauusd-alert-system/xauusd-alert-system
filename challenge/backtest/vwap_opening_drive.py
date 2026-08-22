# -*- coding: utf-8 -*-
"""VWAP breakout and opening drive strategies for US stocks.

Two new strategies tested against ORB baseline:
1. VWAP breakout: enter when price crosses VWAP with volume confirmation
2. Opening drive: enter in the direction of the first 5-min candle after open
"""
import json, os, datetime as dt, math
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
        utc = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc)
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
        print(f"  {label:<40s} 0 trades")
        return
    n, wr, pf, net, avg_r, fees, sim = result
    print(f"  {label:<40s} {n:>4d}  WR={wr:>4.0f}%  PF={pf:>5.2f}  "
          f"net=${net:>+7.0f}  avgR={avg_r:>6.3f}  fees=${fees:>5.0f}  "
          f"pass={sim['passed']}  days={sim['days_used']}")


# ============================================================
# STRATEGY: VWAP breakout
# ============================================================
def run_vwap_breakout(candles, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0,
                      vwap_window=30, min_vol_ratio=1.2):
    """Enter when price crosses above/below VWAP with volume confirmation.
    VWAP is computed from the first `vwap_window` minutes of the session.
    After VWAP is established, a close beyond VWAP +/- buffer triggers entry."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < vwap_window + 10:
            continue
        # Compute VWAP from first N minutes
        vwap_bars = bars[:vwap_window]
        cum_pv = sum(b["close"] * b.get("volume", 1) for b in vwap_bars)
        cum_v = sum(b.get("volume", 1) for b in vwap_bars)
        vwap = cum_pv / cum_v if cum_v > 0 else vwap_bars[-1]["close"]

        # Average volume for confirmation
        avg_vol = sum(b.get("volume", 0) for b in bars[:vwap_window]) / vwap_window

        open_pos = None
        took = False
        # Track VWAP evolution after initial window
        cum_pv_live = cum_pv
        cum_v_live = cum_v
        for i, b in enumerate(bars):
            t = b["time"]
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second

            # Update live VWAP
            if i >= vwap_window:
                cum_pv_live += b["close"] * b.get("volume", 1)
                cum_v_live += b.get("volume", 1)
                vwap = cum_pv_live / cum_v_live if cum_v_live > 0 else vwap

            if i < vwap_window:
                continue  # still building VWAP

            vol = b.get("volume", 0)
            vol_ok = vol >= min_vol_ratio * avg_vol if avg_vol > 0 else True

            if open_pos is None and not took and vol_ok:
                # Long: close above VWAP + 0.1% buffer
                if b["close"] > vwap * 1.001:
                    entry = b["close"] * (1 + SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": 1, "entry": entry, "qty": qty,
                                "stop": entry * (1 - stop_pct),
                                "tp": entry + entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
                # Short: close below VWAP - 0.1% buffer
                elif b["close"] < vwap * 0.999:
                    entry = b["close"] * (1 - SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True

            if open_pos is None or t == open_pos["bar"]:
                continue
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos)
                open_pos = None
                break
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


# ============================================================
# STRATEGY: Opening drive
# ============================================================
def run_opening_drive(candles, drive_bars=5, stop_pct=0.005, tp_ratio=1.5,
                      risk_per_trade=5.0, min_body_ratio=0.5):
    """Enter in the direction of the first `drive_bars` 1-min candles.
    The opening drive is the net move in the first N minutes.
    If the first N bars form a strong directional candle (body > 0.5 * range),
    enter on the close of the Nth bar. Stop beyond the opposite extreme."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < drive_bars + 10:
            continue
        # Opening drive: aggregate first N bars
        drive_b = bars[:drive_bars]
        drive_open = drive_b[0]["open"]
        drive_close = drive_b[-1]["close"]
        drive_high = max(b["high"] for b in drive_b)
        drive_low = min(b["low"] for b in drive_b)
        drive_range = drive_high - drive_low
        drive_body = abs(drive_close - drive_open)

        if drive_range <= 0 or drive_body < min_body_ratio * drive_range:
            continue  # no clear drive

        # Direction from the drive
        if drive_close > drive_open:
            bias = 1  # long
            entry = drive_close * (1 + SLIP)
            stop = drive_low * (1 - SLIP)
        else:
            bias = -1  # short
            entry = drive_close * (1 - SLIP)
            stop = drive_high * (1 + SLIP)

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


# ============================================================
# STRATEGY: Opening drive + VWAP confirmation
# ============================================================
def run_drive_vwap(candles, drive_bars=5, stop_pct=0.005, tp_ratio=1.5,
                   risk_per_trade=5.0, min_body_ratio=0.5):
    """Opening drive + VWAP filter: only take the drive direction if VWAP
    confirms (price above VWAP for long, below for short)."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < drive_bars + 30:
            continue
        # VWAP from first 30 min
        vwap_b = bars[:30]
        cum_pv = sum(b["close"] * b.get("volume", 1) for b in vwap_b)
        cum_v = sum(b.get("volume", 1) for b in vwap_b)
        vwap = cum_pv / cum_v if cum_v > 0 else vwap_b[-1]["close"]

        # Opening drive
        drive_b = bars[:drive_bars]
        drive_open = drive_b[0]["open"]
        drive_close = drive_b[-1]["close"]
        drive_high = max(b["high"] for b in drive_b)
        drive_low = min(b["low"] for b in drive_b)
        drive_range = drive_high - drive_low
        drive_body = abs(drive_close - drive_open)

        if drive_range <= 0 or drive_body < min_body_ratio * drive_range:
            continue

        # VWAP filter: long only above VWAP, short only below
        if drive_close > drive_open and drive_close > vwap:
            bias = 1
            entry = drive_close * (1 + SLIP)
            stop = drive_low * (1 - SLIP)
        elif drive_close < drive_open and drive_close < vwap:
            bias = -1
            entry = drive_close * (1 - SLIP)
            stop = drive_high * (1 + SLIP)
        else:
            continue  # VWAP doesn't confirm

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


# ============================================================
# ORB baseline (for comparison)
# ============================================================
def run_orb_baseline(candles, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0):
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
        open_pos = None; took = False
        for b in bars:
            t = b["time"]
            if t < range_end:
                continue
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
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
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
    print(f"Loading {len(tickers)} tickers...")
    all_candles = {}
    for t in tickers:
        all_candles[t] = load_candles(t)
    total_bars = sum(len(c) for c in all_candles.values())
    print(f"Total bars: {total_bars:,}\n")

    # === Main comparison ===
    print("=" * 105)
    print("STRATEGY COMPARISON: VWAP BREAKOUT vs OPENING DRIVE vs ORB BASELINE")
    print("=" * 105)
    print(f"  {'Strategy':<40s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s} {'fees$':>6s} {'pass':>5s}")
    print(f"  {'-'*95}")

    strategies = [
        ("ORB baseline (30m range)", run_orb_baseline),
        ("VWAP breakout (30m init)", run_vwap_breakout),
        ("VWAP breakout (15m init)", lambda c: run_vwap_breakout(c, vwap_window=15)),
        ("Opening drive (5 bars)", run_opening_drive),
        ("Opening drive (10 bars)", lambda c: run_opening_drive(c, drive_bars=10)),
        ("Opening drive (15 bars)", lambda c: run_opening_drive(c, drive_bars=15)),
        ("Drive + VWAP confirm (5)", run_drive_vwap),
        ("Drive + VWAP confirm (10)", lambda c: run_drive_vwap(c, drive_bars=10)),
    ]

    results = {}
    for label, fn in strategies:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += fn(c)
        r = row_stats(all_trades)
        print_row(label, r)
        results[label] = (all_trades, r)

    # === Opening drive body ratio sweep ===
    print(f"\n{'=' * 105}")
    print("OPENING DRIVE: BODY RATIO SWEEP")
    print("=" * 105)
    print(f"  {'min_body':<10s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*50}")
    for br in [0.3, 0.4, 0.5, 0.6, 0.7]:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += run_opening_drive(c, min_body_ratio=br)
        r = row_stats(all_trades)
        if r is None:
            print(f"  {br:>8.1f}   0 trades")
        else:
            n, wr, pf, net, avg_r, fees, sim = r
            print(f"  {br:>8.1f}  {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}")

    # === TP ratio sweep for opening drive ===
    print(f"\n{'=' * 105}")
    print("OPENING DRIVE: TP RATIO SWEEP (drive=5, body=0.5)")
    print("=" * 105)
    print(f"  {'tp_ratio':<10s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*50}")
    for tp in [1.0, 1.5, 2.0, 2.5, 3.0]:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += run_opening_drive(c, tp_ratio=tp)
        r = row_stats(all_trades)
        if r is None:
            print(f"  {tp:>8.1f}   0 trades")
        else:
            n, wr, pf, net, avg_r, fees, sim = r
            print(f"  {tp:>8.1f}  {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}")

    # === Per-ticker breakdown for best strategies ===
    print(f"\n{'=' * 105}")
    print("PER-TICKER BREAKDOWN (Opening drive 5-bars, body=0.5, tp=1.5)")
    print("=" * 105)
    print(f"  {'Ticker':<10s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*50}")
    for t in tickers:
        c = all_candles[t]
        trades = run_opening_drive(c)
        if not trades:
            continue
        n, wr, pf, net, avg_r, fees, sim = row_stats(trades)
        print(f"  {t:<10s} {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}")

    # === Day-by-day sim for best ===
    print(f"\n{'=' * 105}")
    print("DAY-BY-DAY: Opening drive (5 bars, body=0.5, tp=1.5)")
    print("=" * 105)
    all_trades = []
    for t, c in all_candles.items():
        all_trades += run_opening_drive(c)
    day_pnl = {}
    for tr in all_trades:
        d = dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date()
        day_pnl.setdefault(d, 0.0)
        day_pnl[d] += tr["pnl"]
    equity = 1000.0
    for d in sorted(day_pnl):
        equity += day_pnl[d]
        bar = "+" * min(20, int(max(0, day_pnl[d] * 2))) if day_pnl[d] > 0 else \
              "-" * min(20, int(max(0, -day_pnl[d] * 2)))
        print(f"  {d}  ${day_pnl[d]:>+7.2f}  ${equity:>8.2f}  {bar}")
    sim = simulate(all_trades)
    print(f"\n  Final: equity=${sim['equity_end']:.0f}  pass={sim['passed']}  days={sim['days_used']}")


if __name__ == "__main__":
    main()
