# -*- coding: utf-8 -*-
"""ORB backtest v2 — honest fills: entry on breakout bar, exits checked from NEXT bar,
first-breakout-per-day, slippage, realistic fees. Plus challenge simulation."""
import io, sys, json, os, datetime as dt
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"
OPEN_SEC = 13 * 3600 + 30 * 60
CLOSE_SEC = 19 * 3600 + 55 * 60
FLAT_SEC = 19 * 3600 + 50 * 60
SLIP = 0.0005  # 5 bps slippage per side

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

def run_strategy(candles, range_minutes=30, stop_pct=0.005, tp_ratio=1.5, risk_per_trade=5.0,
                 max_positions=1, slip=SLIP, true_orb=False, fill_mode="hybrid"):
    # fill_mode for non-true_orb: "limit"=fill at rng level even on gap (phantom),
    # "hybrid"=limit fills on touch, gap is chased at open, "open"=always chase at bar open
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
        day_flat = False
        took_trade = False
        for b in bars:
            t = b["time"]
            if t < range_end:
                continue
            if open_pos is None and not took_trade:
                if b["high"] >= rng_high:
                    if true_orb:
                        risk = rng_high - rng_low
                        entry = rng_high * (1 + slip)
                        qty = risk_per_trade / risk
                        open_pos = {"side": 1, "entry": entry, "qty": qty,
                                    "stop": rng_low * (1 - slip), "tp": entry + risk * tp_ratio,
                                    "entry_bar": t}
                    else:
                        if fill_mode == "limit":
                            entry = rng_high * (1 + slip)
                        elif fill_mode == "open":
                            entry = b["open"] * (1 + slip)
                        else:  # hybrid
                            entry = (rng_high if b["open"] < rng_high else b["open"]) * (1 + slip)
                        qty = (risk_per_trade / stop_pct) / entry
                        open_pos = {"side": 1, "entry": entry, "qty": qty,
                                    "stop": entry * (1 - stop_pct),
                                    "tp": entry * (1 + stop_pct * tp_ratio),
                                    "entry_bar": t}
                    took_trade = True
                elif b["low"] <= rng_low:
                    if true_orb:
                        risk = rng_high - rng_low
                        entry = rng_low * (1 - slip)
                        qty = risk_per_trade / risk
                        open_pos = {"side": -1, "entry": entry, "qty": qty,
                                    "stop": rng_high * (1 + slip), "tp": entry - risk * tp_ratio,
                                    "entry_bar": t}
                    else:
                        if fill_mode == "limit":
                            entry = rng_low * (1 - slip)
                        elif fill_mode == "open":
                            entry = b["open"] * (1 - slip)
                        else:  # hybrid
                            entry = (rng_low if b["open"] > rng_low else b["open"]) * (1 - slip)
                        qty = (risk_per_trade / stop_pct) / entry
                        open_pos = {"side": -1, "entry": entry, "qty": qty,
                                    "stop": entry * (1 + stop_pct),
                                    "tp": entry * (1 - stop_pct * tp_ratio),
                                    "entry_bar": t}
                    took_trade = True
            if open_pos is None:
                continue
            if t == open_pos["entry_bar"]:
                continue  # no same-bar exit
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                open_pos["exit_price"] = b["close"] * (1 - slip * open_pos["side"])
                open_pos["exit_ts"] = t
                open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                                  - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                trades.append(open_pos)
                open_pos = None
                day_flat = True
                break
            if open_pos["side"] == 1:
                if b["low"] * (1 - slip) <= open_pos["stop"]:
                    open_pos["exit_price"] = open_pos["stop"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + slip) >= open_pos["tp"]:
                    open_pos["exit_price"] = open_pos["tp"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + slip) >= open_pos["stop"]:
                    open_pos["exit_price"] = open_pos["stop"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["entry"] - open_pos["exit_price"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - slip) <= open_pos["tp"]:
                    open_pos["exit_price"] = open_pos["tp"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["entry"] - open_pos["exit_price"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None and not day_flat:
            open_pos["exit_price"] = bars[-1]["close"] * (1 - slip * open_pos["side"])
            open_pos["exit_ts"] = bars[-1]["time"]
            open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                              - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
            trades.append(open_pos)
    return trades

def simulate(all_trades, start=1000.0, daily_stop=25.0, total_stop=60.0, profit_lock=20.0,
             target=80.0, min_days=5):
    day_pnl = {}
    for tr in all_trades:
        d = dt.datetime.fromtimestamp(tr["exit_ts"], dt.timezone.utc).date()
        day_pnl.setdefault(d, 0.0)
        day_pnl[d] += tr["pnl"]
    equity = start
    curve = []
    failed = passed = False
    stop_day = None
    for d in sorted(day_pnl):
        equity += day_pnl[d]
        curve.append((str(d), day_pnl[d], equity))
        if equity <= start - total_stop:
            failed, stop_day = True, str(d)
            break
        if day_pnl[d] <= -daily_stop:
            failed, stop_day = True, str(d)
            break
        if equity >= start + target and len(curve) >= min_days:
            passed = True
            break
    return {"equity_end": equity, "curve": curve, "passed": passed, "failed": failed,
            "stop_day": stop_day, "days_used": len(curve)}

def stats(trades):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = sum(t["pnl"] for t in losses)
    return {"n": n, "win_rate": len(wins) / n * 100 if n else 0,
            "gw": gw, "gl": gl, "net": gw + gl, "pf": gw / max(1e-9, abs(gl)) if gl else float("inf"),
            "avg": (gw + gl) / n if n else 0}

def main():
    with open(os.path.join(BASE, "symbols.json"), encoding="utf-8") as f:
        symbols = json.load(f)
    print("=== fill-mode comparison (range=30 stop=0.5% tp=1.5R, fixed-stop model) ===")
    for mode in ["limit", "hybrid", "open"]:
        all_trades = []
        for t in symbols:
            candles = load_candles(t)
            all_trades += run_strategy(candles, fill_mode=mode)
        res = simulate(all_trades)
        s = stats(all_trades)
        print(f"fill={mode}: trades={s['n']} wr={s['win_rate']:.0f}% pf={s['pf']:.2f} "
              f"net=${s['net']:+.0f} -> end=${res['equity_end']:.0f} "
              f"pass={res['passed']} fail={res['failed']} days={res['days_used']}")

    print("\n=== config sweep (hybrid fill, fixed stop, slippage 5bps) ===")
    for rm, sp, tr_ratio in [(15, 0.005, 1.5), (30, 0.005, 1.5), (30, 0.005, 2.0),
                             (60, 0.005, 1.5), (30, 0.008, 1.5), (15, 0.008, 2.0), (60, 0.008, 2.0)]:
        all_trades = []
        for t in symbols:
            candles = load_candles(t)
            all_trades += run_strategy(candles, range_minutes=rm, stop_pct=sp, tp_ratio=tr_ratio)
        res = simulate(all_trades)
        s = stats(all_trades)
        print(f"range={rm} stop={sp*100:.1f}% tp={tr_ratio}: trades={s['n']} wr={s['win_rate']:.0f}% "
              f"pf={s['pf']:.2f} net=${s['net']:+.0f} -> end=${res['equity_end']:.0f} "
              f"pass={res['passed']} fail={res['failed']} days={res['days_used']}")

    print("\n=== per symbol (hybrid range=30 stop=0.5% tp=1.5R) ===")
    for t in symbols:
        candles = load_candles(t)
        trs = run_strategy(candles)
        if not trs:
            print(f"  {t}: 0 trades")
            continue
        s = stats(trs)
        print(f"  {t}: {s['n']} trades wr={s['win_rate']:.0f}% pf={s['pf']:.2f} net=${s['net']:+.0f}")

    print("\n=== challenge sim detail (hybrid range=30 stop=0.5% tp=1.5R) ===")
    all_trades = []
    for t in symbols:
        candles = load_candles(t)
        all_trades += run_strategy(candles)
    res = simulate(all_trades)
    for row in res["curve"]:
        print("  ", row[0], "day_pnl=", round(row[1], 2), "equity=", round(row[2], 2))

if __name__ == "__main__":
    main()