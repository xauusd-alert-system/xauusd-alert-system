# -*- coding: utf-8 -*-
"""Quick exploratory sweep: side filter, exit mode, gap filter — using honest hybrid fills."""
import json, os, sys, datetime as dt
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")
from challenge.backtest.orb_backtest import load_candles, simulate, stats, fee, build_days, OPEN_SEC, CLOSE_SEC, FLAT_SEC, SLIP

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"

def run(candles, rm=30, sp=0.005, tr=1.5, side="both", exit_mode="tp", max_gap=0.003):
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
        open_pos = None
        day_flat = False
        took = False
        for b in bars:
            t = b["time"]
            if t < range_end:
                continue
            if open_pos is None and not took:
                gap = (b["open"] - rh) / rh
                if b["high"] >= rh and side in ("both", "long") and gap <= max_gap:
                    entry = (rh if b["open"] < rh else b["open"]) * (1 + SLIP)
                    qty = (5.0 / sp) / entry
                    open_pos = {"side": 1, "entry": entry, "qty": qty,
                                "stop": entry * (1 - sp), "tp": entry * (1 + sp * tr), "entry_bar": t}
                    took = True
                elif b["low"] <= rl and side in ("both", "short") and -gap <= max_gap:
                    entry = (rl if b["open"] > rl else b["open"]) * (1 - SLIP)
                    qty = (5.0 / sp) / entry
                    open_pos = {"side": -1, "entry": entry, "qty": qty,
                                "stop": entry * (1 + sp), "tp": entry * (1 - sp * tr), "entry_bar": t}
                    took = True
            if open_pos is None:
                continue
            if t == open_pos["entry_bar"]:
                continue
            utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            sec = utc.hour * 3600 + utc.minute * 60 + utc.second
            if sec >= FLAT_SEC:
                open_pos["exit_price"] = b["close"] * (1 - SLIP * open_pos["side"])
                open_pos["exit_ts"] = t
                open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                                  - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                trades.append(open_pos)
                open_pos = None
                day_flat = True
                break
            if open_pos["side"] == 1:
                if b["low"] * (1 - SLIP) <= open_pos["stop"]:
                    open_pos["exit_price"] = open_pos["stop"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif exit_mode == "tp" and b["high"] * (1 + SLIP) >= open_pos["tp"]:
                    open_pos["exit_price"] = open_pos["tp"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
            else:
                if b["high"] * (1 + SLIP) >= open_pos["stop"]:
                    open_pos["exit_price"] = open_pos["stop"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["entry"] - open_pos["exit_price"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
                elif exit_mode == "tp" and b["low"] * (1 - SLIP) <= open_pos["tp"]:
                    open_pos["exit_price"] = open_pos["tp"]
                    open_pos["exit_ts"] = t
                    open_pos["pnl"] = (open_pos["entry"] - open_pos["exit_price"]) * open_pos["qty"] \
                                      - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
                    trades.append(open_pos)
                    open_pos = None
        if open_pos is not None and not day_flat:
            open_pos["exit_price"] = bars[-1]["close"] * (1 - SLIP * open_pos["side"])
            open_pos["exit_ts"] = bars[-1]["time"]
            open_pos["pnl"] = (open_pos["exit_price"] - open_pos["entry"]) * open_pos["side"] * open_pos["qty"] \
                              - fee(open_pos["entry"], open_pos["qty"]) - fee(open_pos["exit_price"], open_pos["qty"])
            trades.append(open_pos)
    return trades

if __name__ == "__main__":
    symbols = json.load(open(os.path.join(BASE, "symbols.json"), encoding="utf-8"))
    print("=== side x exit-mode sweep (hybrid fill, rm=30 sp=0.5% tp=1.5R) ===")
    for side in ["both", "long", "short"]:
        for exit_mode in ["tp", "close"]:
            all_t = []
            for t in symbols:
                all_t += run(load_candles(t), side=side, exit_mode=exit_mode)
            res = simulate(all_t)
            s = stats(all_t)
            print("side={:5s} exit={:5s}: trades={:4d} wr={:.0f}% pf={:.2f} net=${:+.0f} "
                  "end=${:.0f} pass={} fail={} days={}".format(
                      side, exit_mode, s["n"], s["win_rate"], s["pf"], s["net"],
                      res["equity_end"], res["passed"], res["failed"], res["days_used"]))
    print("\n=== gap filter sweep (both, tp exit, max_gap) ===")
    for mg in [0.001, 0.002, 0.003, 0.005, 1.0]:
        all_t = []
        for t in symbols:
            all_t += run(load_candles(t), max_gap=mg)
        res = simulate(all_t)
        s = stats(all_t)
        print("max_gap={:.3f}: trades={:4d} wr={:.0f}% pf={:.2f} net=${:+.0f} end=${:.0f} "
              "pass={} fail={} days={}".format(mg, s["n"], s["win_rate"], s["pf"], s["net"],
                                               res["equity_end"], res["passed"], res["failed"], res["days_used"]))