# -*- coding: utf-8 -*-
"""ORB backtest: baseline vs enhanced (volume confirmation + candle close).

Compares the original ORB strategy against the enhanced version with:
- Volume filter: breakout bar volume >= 1.3x average
- Close confirmation: breakout bar must close beyond range (not just wick)
- Session-time buckets: prime (19:00-00:15) vs degraded (00:15-00:45)
- Breakeven + partial close management
"""
import io, sys, json, os, datetime as dt
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"
OPEN_SEC = 13 * 3600 + 30 * 60   # 13:30 UTC = 18:30 local
CLOSE_SEC = 19 * 3600 + 55 * 60  # 19:55 UTC = 00:55 local
FLAT_SEC = 19 * 3600 + 50 * 60   # flatten at 19:50 UTC
SLIP = 0.0005                     # 5 bps slippage per side

# Session buckets (UTC)
PRIME_START = 14 * 3600          # 14:00 UTC = 19:00 local (30min after open)
DEGRADED_START = 18 * 3600 + 15 * 60  # 18:15 UTC = 00:15 local (last 40min)


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
    if utc_sec >= DEGRADED_START:
        return "degraded"
    if utc_sec >= PRIME_START:
        return "prime"
    return "normal"


def _avg_volume(vols, lookback=20):
    """Simple moving average of volume over the last `lookback` observations.
    Accepts either a list of floats (volume values) or a list of bar dicts."""
    if len(vols) < 3:
        return 1.0
    recent = vols[-lookback:]
    # Handle both raw volume floats and bar dicts
    if recent and isinstance(recent[0], dict):
        recent = [b.get("volume", 0) for b in recent]
    return sum(recent) / len(recent) if recent else 1.0


def run_baseline(candles, range_minutes=30, stop_pct=0.005, tp_ratio=1.5,
                 risk_per_trade=5.0, slip=SLIP):
    """Original ORB: no volume filter, no close confirmation."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < range_minutes + 5:
            continue
        range_end = bars[0]["time"] + range_minutes * 60
        rb = [b for b in bars if b["time"] < range_end]
        if not rb:
            continue
        rng_high = max(b["high"] for b in rb)
        rng_low = min(b["low"] for b in rb)
        if rng_high <= rng_low:
            continue
        open_pos = None
        took_trade = False
        for b in bars:
            t = b["time"]
            if t < range_end:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if open_pos is None and not took_trade:
                if b["high"] >= rng_high:
                    entry = (rng_high if b["open"] < rng_high else b["open"]) * (1 + slip)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": 1, "entry": entry, "qty": qty,
                                "stop": entry * (1 - stop_pct),
                                "tp": entry + (entry * stop_pct) * tp_ratio,
                                "entry_bar": t, "session_bucket": _session_bucket(sec)}
                    took_trade = True
                elif b["low"] <= rng_low:
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - slip)
                    qty = (risk_per_trade / stop_pct) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + stop_pct),
                                "tp": entry - (entry * stop_pct) * tp_ratio,
                                "entry_bar": t, "session_bucket": _session_bucket(sec)}
                    took_trade = True
            if open_pos is None:
                continue
            if t == open_pos["entry_bar"]:
                continue  # no same-bar exit
            # Check exits
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


def run_enhanced(candles, range_minutes=30, stop_pct=0.005, tp_ratio=1.5,
                 risk_per_trade=5.0, slip=SLIP,
                 min_vol_ratio=1.3, require_close=True,
                 use_be=True, use_partial=True):
    """Enhanced ORB: volume confirmation + close confirmation + BE + partial."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < range_minutes + 5:
            continue
        range_end = bars[0]["time"] + range_minutes * 60
        rb = [b for b in bars if b["time"] < range_end]
        if not rb:
            continue
        rng_high = max(b["high"] for b in rb)
        rng_low = min(b["low"] for b in rb)
        if rng_high <= rng_low:
            continue
        open_pos = None
        took_trade = False
        vol_history = []  # accumulate volume during range for avg
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
                # --- LONG breakout ---
                if b["high"] >= rng_high:
                    # Volume filter
                    if len(vol_history) >= 3 and vol_ratio < min_vol_ratio:
                        took_trade = True  # skip this symbol for the day
                        continue
                    # Close confirmation: close must be above range high
                    if require_close and b["close"] <= rng_high:
                        # Only wick, not close — skip
                        continue
                    entry = (rng_high if b["open"] < rng_high else b["open"]) * (1 + slip)
                    qty = (risk_per_trade / stop_pct) / entry
                    risk_dist = entry * stop_pct
                    open_pos = {
                        "side": 1, "entry": entry, "qty": qty,
                        "stop": entry * (1 - stop_pct),
                        "tp": entry + risk_dist * tp_ratio,
                        "entry_bar": t, "session_bucket": bucket,
                        "volume_ratio": vol_ratio,
                        "risk_dist": risk_dist,
                        "be_moved": False, "partial_closed": False,
                    }
                    took_trade = True
                # --- SHORT breakout ---
                elif b["low"] <= rng_low:
                    if len(vol_history) >= 3 and vol_ratio < min_vol_ratio:
                        took_trade = True
                        continue
                    if require_close and b["close"] >= rng_low:
                        continue
                    entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - slip)
                    qty = (risk_per_trade / stop_pct) / entry
                    risk_dist = entry * stop_pct
                    open_pos = {
                        "side": -1, "entry": entry, "qty": qty,
                        "stop": entry * (1 + stop_pct),
                        "tp": entry - risk_dist * tp_ratio,
                        "entry_bar": t, "session_bucket": bucket,
                        "volume_ratio": vol_ratio,
                        "risk_dist": risk_dist,
                        "be_moved": False, "partial_closed": False,
                    }
                    took_trade = True

            if open_pos is None:
                continue
            if t == open_pos["entry_bar"]:
                continue  # no same-bar exit

            risk_dist = open_pos["risk_dist"]
            entry = open_pos["entry"]

            # --- Breakeven at 0.5R ---
            if use_be and not open_pos["be_moved"]:
                if open_pos["side"] == 1 and (b["high"] - entry) >= 0.5 * risk_dist:
                    open_pos["stop"] = entry  # move to BE
                    open_pos["be_moved"] = True
                elif open_pos["side"] == -1 and (entry - b["low"]) >= 0.5 * risk_dist:
                    open_pos["stop"] = entry
                    open_pos["be_moved"] = True

            # --- Partial close 50% at 1R ---
            if use_partial and not open_pos["partial_closed"]:
                if open_pos["side"] == 1 and (b["high"] - entry) >= risk_dist:
                    open_pos["partial_closed"] = True
                    open_pos["qty"] = max(0.01, open_pos["qty"] * 0.5)
                elif open_pos["side"] == -1 and (entry - b["low"]) >= risk_dist:
                    open_pos["partial_closed"] = True
                    open_pos["qty"] = max(0.01, open_pos["qty"] * 0.5)

            # --- Exit checks ---
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
    curve = []
    failed = passed = False
    for d in sorted(day_pnl):
        equity += day_pnl[d]
        curve.append((str(d), round(day_pnl[d], 2), round(equity, 2)))
        if equity <= start - total_stop:
            failed = True
            break
        if day_pnl[d] <= -daily_stop:
            failed = True
            break
        if equity >= start + target and len(curve) >= min_days:
            passed = True
            break
    return {"equity_end": round(equity, 2), "curve": curve, "passed": passed,
            "failed": failed, "days_used": len(curve)}


def detailed_stats(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = sum(t["pnl"] for t in losses)
    # R-multiples (pnl / risk_per_trade)
    r_mults = []
    for t in trades:
        entry = t["entry"]
        stop = t["stop"]
        risk_usd = abs(entry - stop) * t["qty"]
        if risk_usd > 0:
            r_mults.append(t["pnl"] / risk_usd)
    avg_r = sum(r_mults) / len(r_mults) if r_mults else 0
    # By bucket
    by_bucket = defaultdict(list)
    for t in trades:
        by_bucket[t.get("session_bucket", "unknown")].append(t)
    # By exit reason
    by_reason = defaultdict(int)
    for t in trades:
        by_reason[t.get("exit_reason", "?")] += 1
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "pf": gw / max(1e-9, abs(gl)) if gl else float("inf"),
        "net": round(gw + gl, 2),
        "avg_r": round(avg_r, 3),
        "total_fees": round(sum(fee(t["entry"], t["qty"]) + fee(t["exit_price"], t["qty"]) for t in trades), 2),
        "by_bucket": {b: {"n": len(ts), "wr": round(100 * sum(1 for t in ts if t["pnl"] > 0) / len(ts), 1)}
                      for b, ts in by_bucket.items()},
        "by_reason": dict(by_reason),
        "be_triggered": sum(1 for t in trades if t.get("be_moved")),
        "partial_triggered": sum(1 for t in trades if t.get("partial_closed")),
    }


def main():
    with open(os.path.join(BASE, "symbols.json"), encoding="utf-8") as f:
        symbols = json.load(f)
    # Only use tickers that have candle data
    candle_dir = os.path.join(BASE, "candles")
    symbols = [t for t in symbols if os.path.exists(os.path.join(candle_dir, t + ".json"))]

    print("=" * 80)
    print("ORB BACKTEST: BASELINE vs ENHANCED (volume+close+BE+partial)")
    print(f"Tickers: {len(symbols)}  |  Full dataset  |  Stop: 0.5%  |  TP: 1.5R")
    print("=" * 80)

    all_baseline = []
    all_enhanced = []

    for ticker in sorted(symbols):
        candles = load_candles(ticker)
        candles_4w = candles  # use all available data

        base = run_baseline(candles_4w)
        ench = run_enhanced(candles_4w)

        all_baseline.extend(base)
        all_enhanced.extend(ench)

        bs = detailed_stats(base) if base else {"n": 0}
        es = detailed_stats(ench) if ench else {"n": 0}
        print(f"\n{ticker}:")
        print(f"  Baseline: {bs['n']:3d} trades  WR={bs.get('win_rate',0):.0f}%  "
              f"PF={bs.get('pf',0):.2f}  net=${bs.get('net',0):+.0f}  avgR={bs.get('avg_r',0):.3f}")
        print(f"  Enhanced: {es['n']:3d} trades  WR={es.get('win_rate',0):.0f}%  "
              f"PF={es.get('pf',0):.2f}  net=${es.get('net',0):+.0f}  avgR={es.get('avg_r',0):.3f}"
              f"  BE={es.get('be_triggered',0)}  partial={es.get('partial_triggered',0)}")

    print("\n" + "=" * 80)
    print("AGGREGATE RESULTS")
    print("=" * 80)

    bs = detailed_stats(all_baseline)
    es = detailed_stats(all_enhanced)

    print(f"\n{'Metric':<25s} {'Baseline':>15s} {'Enhanced':>15s} {'Delta':>12s}")
    print("-" * 70)
    for key, label in [("n", "Trades"), ("win_rate", "Win Rate %"), ("pf", "Profit Factor"),
                       ("net", "Net P&L $"), ("avg_r", "Avg R"), ("total_fees", "Total Fees $")]:
        bv = bs.get(key, 0)
        ev = es.get(key, 0)
        delta = ev - bv
        fmt = ".0f" if key in ("n", "net", "total_fees") else ".1f" if key == "win_rate" else ".3f"
        print(f"  {label:<23s} {bv:>15{fmt}} {ev:>15{fmt}} {delta:>+12{fmt}}")

    print(f"\n  Session buckets (enhanced):")
    for bucket, info in sorted(es.get("by_bucket", {}).items()):
        print(f"    {bucket:12s}: {info['n']:3d} trades  WR={info['wr']:.0f}%")

    print(f"\n  Exit reasons (enhanced):")
    for reason, count in sorted(es.get("by_reason", {}).items()):
        print(f"    {reason:12s}: {count}")

    # Challenge simulation
    print("\n" + "=" * 80)
    print("CHALLENGE SIMULATION ($1000 start, $25 daily stop, $60 total stop, +$80 target)")
    print("=" * 80)

    sim_b = simulate(all_baseline)
    sim_e = simulate(all_enhanced)

    print(f"\n  Baseline: equity=${sim_b['equity_end']:.0f}  days={sim_b['days_used']}  "
          f"pass={sim_b['passed']}  fail={sim_b['failed']}")
    print(f"  Enhanced: equity=${sim_e['equity_end']:.0f}  days={sim_e['days_used']}  "
          f"pass={sim_e['passed']}  fail={sim_e['failed']}")

    print("\n  Enhanced equity curve:")
    for day, dpnl, eq in sim_e["curve"]:
        bar = "+" * int(max(0, dpnl)) if dpnl > 0 else "-" * int(max(0, -dpnl))
        print(f"    {day}  day_pnl=${dpnl:>+7.2f}  equity=${eq:>8.2f}  {bar}")


if __name__ == "__main__":
    main()
