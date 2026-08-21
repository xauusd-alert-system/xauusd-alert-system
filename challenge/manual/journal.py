# -*- coding: utf-8 -*-
"""Trade journal and analytics (ТЗ §7): CSV log, daily summaries, weekly metrics.

Columns follow the ТЗ §7.1 layout. The journal is a plain append-only CSV so it
can be reviewed in any spreadsheet; analytics read it back with the standard
library only.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from collections import OrderedDict

DEFAULT_JOURNAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "manual", "journal.csv")

HEADER = ["num", "date", "time", "instrument", "direction", "setup_class",
          "entry_price", "stop", "target", "risk_usd", "risk_pct", "result_usd",
          "result_r", "outcome", "by_plan", "violation", "comment",
          # RESEARCH 2026-08-22: expanded columns for commission + session + regime
          "commission_usd", "session_bucket", "time_in_trade_min", "volume_ratio",
          "regime"]


def _ensure(path: str) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADER)


def add_trade(path: str, date, time, instrument, direction, setup_class,
              entry_price, stop, target, risk_usd, risk_pct, result_usd=None,
              result_r=None, outcome="", by_plan="да", violation="", comment="",
              num: int | None = None, commission_usd: float = 0.0,
              session_bucket: str = "", time_in_trade_min: float = 0.0,
              volume_ratio: float = 0.0, regime: str = "") -> int:
    """Append one trade. Returns its number."""
    _ensure(path)
    if num is None:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        num = int(rows[-1]["num"]) + 1 if rows else 1
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([num, date, time, instrument, direction, setup_class,
                    entry_price, stop, target, risk_usd, risk_pct,
                    "" if result_usd is None else result_usd,
                    "" if result_r is None else result_r,
                    outcome, by_plan, violation, comment,
                    commission_usd, session_bucket,
                    time_in_trade_min, volume_ratio, regime])
    return num


def close_trade(path: str, num: int, result_usd: float, result_r: float,
                outcome: str, by_plan: str = "да", violation: str = "",
                comment: str = "") -> bool:
    """Fill in the result fields of an already-logged open trade (in-place edit
    of the CSV line). Returns True on success."""
    _ensure(path)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    idx = header.index("num")
    done = False
    for row in rows[1:]:
        if row and row[idx] == str(num):
            row[header.index("result_usd")] = str(result_usd)
            row[header.index("result_r")] = str(result_r)
            row[header.index("outcome")] = outcome
            row[header.index("by_plan")] = by_plan
            row[header.index("violation")] = violation
            row[header.index("comment")] = comment
            done = True
            break
    if done:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    return done


def read(path: str = DEFAULT_JOURNAL) -> list[dict]:
    _ensure(path)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def daily_summary(path: str = DEFAULT_JOURNAL) -> list[dict]:
    """Per-day aggregates: trades, PnL, win rate, avg R, by-plan share,
    commission, session bucket distribution."""
    rows = read(path)
    days = OrderedDict()
    for r in rows:
        days.setdefault(r["date"], []).append(r)
    out = []
    for d, rs in days.items():
        usd = [float(x["result_usd"]) for x in rs if x["result_usd"] not in ("", None)]
        rr = [float(x["result_r"]) for x in rs if x["result_r"] not in ("", None)]
        planned = [x for x in rs if x["by_plan"].strip().lower() in ("да", "yes", "1")]
        wins = [x for x in rs if x["outcome"].strip().upper() in ("W", "WIN")]
        losses = [x for x in rs if x["outcome"].strip().upper() in ("L", "LOSS")]
        # RESEARCH 2026-08-22: new metrics
        commissions = [float(x.get("commission_usd", 0) or 0) for x in rs]
        buckets = [x.get("session_bucket", "") for x in rs if x.get("session_bucket")]
        time_in = [float(x.get("time_in_trade_min", 0) or 0) for x in rs]
        out.append({
            "date": d,
            "trades": len(rs),
            "pnl_usd": round(sum(usd), 2) if usd else 0.0,
            "avg_r": round(sum(rr) / len(rr), 2) if rr else 0.0,
            "win_rate_pct": round(100 * len(wins) / len(rs), 1) if rs else 0.0,
            "losses": len(losses),
            "by_plan_pct": round(100 * len(planned) / len(rs), 1) if rs else 0.0,
            "total_commission_usd": round(sum(commissions), 2),
            "avg_time_in_trade_min": round(sum(time_in) / len(time_in), 1) if time_in else 0.0,
            "session_buckets": dict((b, buckets.count(b)) for b in set(buckets)) if buckets else {},
        })
    return out


def weekly_metrics(path: str = DEFAULT_JOURNAL) -> list[dict]:
    """Weekly aggregates (ТЗ §7.2): by-plan %, avg R, A-class stats, max loss
    streak, max daily drawdown, share of A-setups."""
    rows = read(path)
    weeks = OrderedDict()
    for r in rows:
        try:
            d = dt.date.fromisoformat(r["date"])
        except ValueError:
            continue
        key = d.isocalendar()[:2]  # (ISO year, week)
        weeks.setdefault(key, []).append(r)

    out = []
    for key, rs in weeks.items():
        rr = [float(x["result_r"]) for x in rs if x["result_r"] not in ("", None)]
        a_rs = [x for x in rs if x["setup_class"].strip().upper() == "A"]
        a_rr = [float(x["result_r"]) for x in a_rs if x["result_r"] not in ("", None)]
        planned = [x for x in rs if x["by_plan"].strip().lower() in ("да", "yes", "1")]
        wins_a = [x for x in a_rs if x["outcome"].strip().upper() in ("W", "WIN")]

        max_streak = streak = 0
        for x in rs:
            if x["outcome"].strip().upper() in ("L", "LOSS"):
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0

        by_day = {}
        for x in rs:
            usd = float(x["result_usd"]) if x["result_usd"] not in ("", None) else 0.0
            by_day[x["date"]] = by_day.get(x["date"], 0.0) + usd
        max_dd = 0.0
        for d, pnl in by_day.items():
            if pnl < 0:
                max_dd = min(max_dd, pnl)

        out.append({
            "iso_week": "%d-W%02d" % key,
            "trades": len(rs),
            "by_plan_pct": round(100 * len(planned) / len(rs), 1) if rs else 0.0,
            "avg_r": round(sum(rr) / len(rr), 2) if rr else 0.0,
            "avg_r_a": round(sum(a_rr) / len(a_rr), 2) if a_rr else 0.0,
            "win_rate_a_pct": round(100 * len(wins_a) / len(a_rs), 1) if a_rs else 0.0,
            "max_loss_streak": max_streak,
            "max_daily_dd_usd": round(max_dd, 2),
            "a_share_pct": round(100 * len(a_rs) / len(rs), 1) if rs else 0.0,
        })
    return out