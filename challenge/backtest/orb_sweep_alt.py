# -*- coding: utf-8 -*-
"""Alt strategy search: 5-min momentum, range-fade mean reversion, VWAP pullback.
Honest fills only: hybrid limit/touch+gap-chase, no same-bar exit, slippage 5bps, fees $1/side min."""
import json, os, sys, datetime as dt
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")
from challenge.backtest.orb_backtest import load_candles, simulate, stats, fee, build_days, FLAT_SEC, SLIP

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"

def resample(candles, minutes=5):
    """1-min -> N-min bars (open=first open, high=max, low=min, close=last close)."""
    out, cur = [], None
    for c in sorted(candles, key=lambda x: x["time"]):
        if cur is None:
            cur = {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "time": c["time"]}
        elif c["time"] < cur["time"] + minutes * 60:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
        else:
            out.append(cur)
            cur = {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "time": c["time"]}
    if cur:
        out.append(cur)
    return out

def momentum_5m(candles, rm_bars=6, sp=0.005, tr=1.5, side="both"):
    bars = resample(candles, 5)
    days = {}
    for b in bars:
        utc = dt.datetime.fromtimestamp(b["time"], dt.timezone.utc)
        days.setdefault(utc.date(), []).append(b)
    trades = []
    for date in sorted(days):
        d = sorted(days[date], key=lambda x: x["time"])
        if len(d) < rm_bars + 2:
            continue
        rb = d[:rm_bars]
        rh = max(b["high"] for b in rb)
        rl = min(b["low"] for b in rb)
        open_pos = None
        took = False
        day_flat = False
        for b in d[rm_bars:]:
            t = b["time"]
            if open_pos is None and not took:
                if b["high"] >= rh and side in ("both", "long"):
                    entry = (rh if b["open"] < rh else b["open"]) * (1 + SLIP)
                    qty = (5.0 / sp) / entry
                    open_pos = {"side": 1, "entry": entry, "qty": qty, "stop": entry * (1 - sp),
                                "tp": entry * (1 + sp * tr), "bar": t}
                    took = True
                elif b["low"] <= rl and side in ("both", "short"):
                    entry = (rl if b["open"] > rl else b["open"]) * (1 - SLIP)
                    qty = (5.0 / sp) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty, "stop": entry * (1 + sp),
                                "tp": entry * (1 - sp * tr), "bar": t}
                    took = True
            if open_pos is None:
                continue
            if t == open_pos["bar"]:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                px = b["close"] * (1 - SLIP * open_pos["side"])
                open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                                  - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                trades.append(open_pos)
                open_pos = None
                day_flat = True
                break
            if open_pos["side"] == 1:
                if b["low"] * (1 - SLIP) <= open_pos["stop"]:
                    px = open_pos["stop"]
                    open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + SLIP) >= open_pos["tp"]:
                    px = open_pos["tp"]
                    open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= open_pos["stop"]:
                    px = open_pos["stop"]
                    open_pos["pnl"] = (open_pos["entry"] - px) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - SLIP) <= open_pos["tp"]:
                    px = open_pos["tp"]
                    open_pos["pnl"] = (open_pos["entry"] - px) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None and not day_flat:
            px = d[-1]["close"] * (1 - SLIP * open_pos["side"])
            open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                              - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
            trades.append(open_pos)
    return trades

def fade_range(candles, rm=30, tp_mode="mid", sp=0.006, tr=1.0):
    """Mean reversion: buy break of range LOW (or sell break of HIGH), exit back inside range."""
    days = build_days(candles)
    trades = []
    for date in sorted(days):
        bars = sorted(days[date], key=lambda c: c["time"])
        if len(bars) < rm + 5:
            continue
        range_end = bars[0]["time"] + rm * 60
        rb = [b for b in bars if b["time"] < range_end]
        if not rb:
            continue
        rh = max(b["high"] for b in rb)
        rl = min(b["low"] for b in rb)
        mid = (rh + rl) / 2
        open_pos = None
        took = False
        day_flat = False
        for b in bars:
            t = b["time"]
            if t < range_end:
                continue
            if open_pos is None and not took:
                if b["low"] <= rl:  # breakdown -> counter-trend LONG
                    entry = min(rl, b["open"]) * (1 + SLIP)
                    qty = (5.0 / sp) / entry
                    stop = rl * (1 - sp)
                    tp = mid if tp_mode == "mid" else rh
                    open_pos = {"side": 1, "entry": entry, "qty": qty, "stop": stop, "tp": tp, "bar": t}
                    took = True
                elif b["high"] >= rh:  # breakout -> counter-trend SHORT
                    entry = max(rh, b["open"]) * (1 - SLIP)
                    qty = (5.0 / sp) / entry
                    stop = rh * (1 + sp)
                    tp = mid if tp_mode == "mid" else rl
                    open_pos = {"side": -1, "entry": entry, "qty": qty, "stop": stop, "tp": tp, "bar": t}
                    took = True
            if open_pos is None:
                continue
            if t == open_pos["bar"]:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                px = b["close"] * (1 - SLIP * open_pos["side"])
                open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                                  - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                trades.append(open_pos)
                open_pos = None
                day_flat = True
                break
            if open_pos["side"] == 1:
                if b["low"] * (1 - SLIP) <= open_pos["stop"]:
                    px = open_pos["stop"]
                    open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif b["high"] * (1 + SLIP) >= open_pos["tp"]:
                    px = open_pos["tp"]
                    open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= open_pos["stop"]:
                    px = open_pos["stop"]
                    open_pos["pnl"] = (open_pos["entry"] - px) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif b["low"] * (1 - SLIP) <= open_pos["tp"]:
                    px = open_pos["tp"]
                    open_pos["pnl"] = (open_pos["entry"] - px) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None and not day_flat:
            px = bars[-1]["close"] * (1 - SLIP * open_pos["side"])
            open_pos["pnl"] = (px - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                              - fee(open_pos["entry"], open_pos["qty"]) - fee(px, open_pos["qty"])
            trades.append(open_pos)
    return trades

if __name__ == "__main__":
    symbols = json.load(open(os.path.join(BASE, "symbols.json"), encoding="utf-8"))
    print("=== 5-min momentum (hybrid fill, rm=30min, sp=0.5%) ===")
    for rm_bars, sp, tr, side in [(6, 0.005, 1.5, "both"), (6, 0.005, 2.0, "both"), (3, 0.005, 1.5, "both"),
                                  (6, 0.008, 1.5, "both"), (6, 0.005, 1.5, "long"), (6, 0.005, 1.5, "short")]:
        all_t = []
        for t in symbols:
            all_t += momentum_5m(load_candles(t), rm_bars, sp, tr, side)
        res = simulate(all_t)
        s = stats(all_t)
        print("rm={}bars sp={:.1f}% tr={} side={:5s}: trades={:4d} wr={:.0f}% pf={:.2f} net=${:+.0f} "
              "end=${:.0f} pass={} fail={} days={}".format(
                  rm_bars, sp * 100, tr, side, s["n"], s["win_rate"], s["pf"], s["net"],
                  res["equity_end"], res["passed"], res["failed"], res["days_used"]))

    print("\n=== range FADE (mean reversion, rm=30) ===")
    for tp_mode in ["mid", "high_low"]:
        for sp in [0.004, 0.006]:
            all_t = []
            for t in symbols:
                all_t += fade_range(load_candles(t), tp_mode=tp_mode, sp=sp)
            res = simulate(all_t)
            s = stats(all_t)
            print("tp={:8s} sp={:.1f}%: trades={:4d} wr={:.0f}% pf={:.2f} net=${:+.0f} "
                  "end=${:.0f} pass={} fail={} days={}".format(
                      tp_mode, sp * 100, s["n"], s["win_rate"], s["pf"], s["net"],
                      res["equity_end"], res["passed"], res["failed"], res["days_used"]))