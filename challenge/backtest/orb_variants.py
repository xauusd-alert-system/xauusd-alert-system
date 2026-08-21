# -*- coding: utf-8 -*-
"""Test alternative ORB approaches: wider range, gap fade, multi-TF confirmation."""
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
            failed = True
            break
        if day_pnl[d] <= -daily_stop:
            failed = True
            break
        if equity >= start + target and days_used >= min_days:
            passed = True
            break
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
# STRATEGY 1: Baseline 30-min ORB
# ============================================================
def run_baseline_30(candles, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0):
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
        took = False
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
                                "tp": entry + entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
                elif b["low"] <= rng_low:
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
            if open_pos is None or t == open_pos["bar"]:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos)
                open_pos = None
                break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
            trades.append(open_pos)
    return trades


# ============================================================
# STRATEGY 2: 60-min range (wider opening range)
# ============================================================
def run_60min_range(candles, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0):
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < 65:
            continue
        range_end = bars[0]["time"] + 60 * 60  # 60 min range
        rb = [b for b in bars if b["time"] < range_end]
        if not rb:
            continue
        rng_high = max(b["high"] for b in rb)
        rng_low = min(b["low"] for b in rb)
        if rng_high <= rng_low:
            continue
        # Wider range = wider stop = fewer but higher-quality breakouts
        open_pos = None
        took = False
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
                                "tp": entry + entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
                elif b["low"] <= rng_low:
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
            if open_pos is None or t == open_pos["bar"]:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos)
                open_pos = None
                break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
            trades.append(open_pos)
    return trades


# ============================================================
# STRATEGY 3: Gap fade (fade the gap at open, target previous close)
# ============================================================
def run_gap_fade(candles, min_gap_pct=0.003, stop_pct=0.005, risk_per_trade=5.0):
    """Fade gaps: if stock gaps up >min_gap_pct from yesterday's close, short it
    targeting prev close. If gaps down, long it. Stop beyond the gap extreme."""
    days = build_days(candles)
    # Build prev-day close map
    sorted_dates = sorted(days.keys())
    prev_close = {}
    for i, d in enumerate(sorted_dates):
        if i > 0:
            prev_bar = days[sorted_dates[i - 1]][-1]
            prev_close[d] = prev_bar["close"]

    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < 10 or date not in prev_close:
            continue
        pc = prev_close[date]
        if pc <= 0:
            continue
        # First bar of session = open price
        session_open = bars[0]["open"]
        gap_pct = (session_open - pc) / pc

        if abs(gap_pct) < min_gap_pct:
            continue  # gap too small

        # Fade direction: gap up -> short, gap down -> long
        if gap_pct > 0:
            bias = -1  # short the gap up
            entry = session_open * (1 - SLIP)
            stop = session_open * (1 + stop_pct)
            target = pc  # target prev close (full gap fill)
        else:
            bias = 1  # long the gap down
            entry = session_open * (1 + SLIP)
            stop = session_open * (1 - stop_pct)
            target = pc

        qty = (risk_per_trade / stop_pct) / entry
        open_pos = {"side": bias, "entry": entry, "qty": qty,
                    "stop": stop, "tp": target, "bar": bars[0]["time"]}

        for b in bars[1:]:
            t = b["time"]
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if open_pos is None:
                break
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos)
                open_pos = None
                break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
            trades.append(open_pos)
    return trades


# ============================================================
# STRATEGY 4: Multi-TF confirmation (15m trend + 5m breakout)
# ============================================================
def run_mtf_confirm(candles, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0):
    """ORB breakout only in the direction of the 15-min trend.
    15m trend = slope of 15-min EMA20. If up, only take long breakouts."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < 35:
            continue
        # Compute 15-min trend from first 90 min
        bars15 = _resample(bars, 15)
        if len(bars15) < 6:
            continue
        closes15 = [b["close"] for b in bars15[:6]]
        ema_fast = _ema(closes15, 3)[-1]
        ema_slow = _ema(closes15, 5)[-1]
        trend = "up" if ema_fast > ema_slow else "down"

        range_end = bars[0]["time"] + 30 * 60
        rb = [b for b in bars if b["time"] < range_end]
        if not rb:
            continue
        rng_high = max(b["high"] for b in rb)
        rng_low = min(b["low"] for b in rb)
        if rng_high <= rng_low:
            continue

        open_pos = None
        took = False
        for b in bars:
            t = b["time"]
            if t < range_end:
                continue
            if open_pos is None and not took:
                if b["high"] >= rng_high and trend == "up":
                    entry = (rng_high if b["open"] < rng_high else b["open"]) * (1 + SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": 1, "entry": entry, "qty": qty,
                                "stop": entry * (1 - stop_pct),
                                "tp": entry + entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
                elif b["low"] <= rng_low and trend == "down":
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - SLIP)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - entry * stop_pct * tp_ratio,
                                "bar": t}
                    took = True
            if open_pos is None or t == open_pos["bar"]:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                _close(open_pos, b["close"], t, SLIP, "eod")
                trades.append(open_pos)
                open_pos = None
                break
            s = open_pos
            if s["side"] == 1:
                if b["low"] * (1 - SLIP) <= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + SLIP) >= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= s["stop"]:
                    _close(open_pos, s["stop"], t, SLIP, "stop")
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - SLIP) <= s["tp"]:
                    _close(open_pos, s["tp"], t, SLIP, "target")
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None:
            _close(open_pos, bars[-1]["close"], bars[-1]["time"], SLIP, "eod")
            trades.append(open_pos)
    return trades


def _resample(bars, minutes):
    out, cur = [], None
    for b in bars:
        if cur is None:
            cur = {"open": b["open"], "high": b["high"], "low": b["low"],
                   "close": b["close"], "time": b["time"], "volume": b.get("volume", 0)}
        elif b["time"] < cur["time"] + minutes * 60:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b.get("volume", 0)
        else:
            out.append(cur)
            cur = {"open": b["open"], "high": b["high"], "low": b["low"],
                   "close": b["close"], "time": b["time"], "volume": b.get("volume", 0)}
    if cur:
        out.append(cur)
    return out


def _ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def main():
    candle_dir = os.path.join(BASE, "candles")
    tickers = sorted(f.replace(".json", "") for f in os.listdir(candle_dir) if f.endswith(".json"))
    print(f"Loading {len(tickers)} tickers...")
    all_candles = {}
    for t in tickers:
        all_candles[t] = load_candles(t)
    total_bars = sum(len(c) for c in all_candles.values())
    print(f"Total bars: {total_bars:,}\n")

    strategies = [
        ("1. Baseline 30-min ORB", run_baseline_30),
        ("2. 60-min wider range ORB", run_60min_range),
        ("3. Gap fade (min 0.3% gap)", lambda c: run_gap_fade(c, min_gap_pct=0.003)),
        ("4. Gap fade (min 0.5% gap)", lambda c: run_gap_fade(c, min_gap_pct=0.005)),
        ("5. Multi-TF confirm (15m trend)", run_mtf_confirm),
    ]

    print("=" * 100)
    print("ORB VARIANT COMPARISON")
    print("=" * 100)
    print(f"  {'Strategy':<40s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s} {'fees$':>6s} {'pass':>5s}")
    print(f"  {'-'*90}")

    for label, strategy_fn in strategies:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += strategy_fn(c)
        print_row(label, row_stats(all_trades))

    # Sweep gap fade thresholds
    print(f"\n{'=' * 100}")
    print("GAP FADE THRESHOLD SWEEP")
    print("=" * 100)
    print(f"  {'min_gap%':<12s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s} {'pass':>5s}")
    print(f"  {'-'*50}")
    for gap in [0.001, 0.002, 0.003, 0.005, 0.008, 0.01]:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += run_gap_fade(c, min_gap_pct=gap)
        result = row_stats(all_trades)
        if result is None:
            print(f"  {gap*100:>8.1f}%   0 trades")
        else:
            n, wr, pf, net, avg_r, fees, sim = result
            print(f"  {gap*100:>8.1f}%  {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}  {str(sim['passed']):>5s}")

    # Sweep stop_pct for gap fade (target is always prev close)
    print(f"\n{'=' * 100}")
    print("GAP FADE: STOP PCT SWEEP (min_gap=0.3%, target=prev close)")
    print("=" * 100)
    print(f"  {'stop%':<10s} {'N':>4s} {'WR%':>5s} {'PF':>6s} {'net$':>8s} {'avgR':>7s}")
    print(f"  {'-'*50}")
    for sp in [0.003, 0.005, 0.008, 0.01]:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += run_gap_fade(c, min_gap_pct=0.003, stop_pct=sp)
        result = row_stats(all_trades)
        if result is None:
            print(f"  {sp*100:>8.1f}%   0 trades")
        else:
            n, wr, pf, net, avg_r, fees, sim = result
            print(f"  {sp*100:>8.1f}%  {n:>4d}  {wr:>4.0f}%  {pf:>5.2f}  ${net:>+7.0f}  {avg_r:>6.3f}")

    # Detailed challenge sim for best variant
    print(f"\n{'=' * 100}")
    print("BEST VARIANTS: DAY-BY-DAY CHALLENGE SIM")
    print("=" * 100)

    best_strategies = [
        ("Gap fade 0.3%", lambda c: run_gap_fade(c, min_gap_pct=0.003)),
        ("Gap fade 0.5%", lambda c: run_gap_fade(c, min_gap_pct=0.005)),
        ("60-min range", run_60min_range),
        ("MTF confirm", run_mtf_confirm),
    ]

    for label, fn in best_strategies:
        all_trades = []
        for t, c in all_candles.items():
            all_trades += fn(c)
        if not all_trades:
            continue
        day_pnl = {}
        for tr in all_trades:
            d = dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date()
            day_pnl.setdefault(d, 0.0)
            day_pnl[d] += tr["pnl"]

        equity = 1000.0
        result = simulate(all_trades)
        print(f"\n  {label}: equity=${result['equity_end']:.0f}  pass={result['passed']}  days={result['days_used']}")
        for d in sorted(day_pnl):
            equity += day_pnl[d]
            bar = "+" * min(20, int(max(0, day_pnl[d] * 2))) if day_pnl[d] > 0 else \
                  "-" * min(20, int(max(0, -day_pnl[d] * 2)))
            print(f"    {d}  ${day_pnl[d]:>+7.2f}  ${equity:>8.2f}  {bar}")


if __name__ == "__main__":
    main()
