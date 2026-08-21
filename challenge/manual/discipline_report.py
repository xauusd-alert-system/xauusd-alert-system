# -*- coding: utf-8 -*-
"""Daily discipline report for the prop challenge (us_stocks audit §6.1/§7).

Combines journal data + outcomes data into a single discipline scorecard:
1. Journal adherence % — are trades being logged properly?
2. Regime breakdown — performance by market trend state
3. Time-bucket stats — prime/normal/degraded session performance
4. Commission drag — total fees as % of gross P&L
5. Checklist compliance — were entry/stop/target planned before entry?
6. Streak analysis — consecutive losses, max drawdown
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from collections import OrderedDict, defaultdict

from challenge.manual.journal import read as journal_read, DEFAULT_JOURNAL


# --- Compliance scoring ---

# Columns that should be filled for a "complete" trade record
REQUIRED_FIELDS = ["entry_price", "stop", "target", "risk_usd"]
PLANNED_FIELDS = ["by_plan", "outcome"]


def _safe_float(val, default=0.0) -> float:
    if val in ("", None):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=0) -> int:
    if val in ("", None):
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _is_filled(val) -> bool:
    return val is not None and str(val).strip() != ""


def compute_adherence(rows: list[dict]) -> dict:
    """Compute journal adherence metrics.

    Adherence = % of trades where ALL required fields are filled AND
    by_plan is 'yes'/'да'. This measures whether the trader is following
    the discipline protocol from the checklist (audit §6.1).
    """
    if not rows:
        return {"total": 0, "complete": 0, "adherence_pct": 0.0,
                "missing_fields": {}, "by_plan_pct": 0.0}

    total = len(rows)
    complete = 0
    missing = defaultdict(int)
    by_plan_count = 0

    for r in rows:
        all_filled = True
        for field in REQUIRED_FIELDS:
            if not _is_filled(r.get(field)):
                missing[field] += 1
                all_filled = False
        if all_filled:
            complete += 1
        if r.get("by_plan", "").strip().lower() in ("да", "yes", "1"):
            by_plan_count += 1

    return {
        "total": total,
        "complete": complete,
        "adherence_pct": round(100 * complete / total, 1) if total else 0.0,
        "missing_fields": dict(missing),
        "by_plan_pct": round(100 * by_plan_count / total, 1) if total else 0.0,
    }


def compute_regime_breakdown(rows: list[dict]) -> dict:
    """Performance breakdown by market regime.

    Regimes: trend_up, trend_down, range, compression, unknown.
    Shows n, win_rate, avg_r, net_pnl for each.
    """
    by_regime = defaultdict(list)
    for r in rows:
        regime = r.get("regime", "").strip() or "unknown"
        by_regime[regime].append(r)

    out = {}
    for regime, trades in sorted(by_regime.items()):
        rr = [_safe_float(x["result_r"]) for x in trades if x.get("result_r") not in ("", None)]
        usd = [_safe_float(x["result_usd"]) for x in trades if x.get("result_usd") not in ("", None)]
        wins = sum(1 for r in trades
                   if r.get("outcome", "").strip().upper() in ("W", "WIN", "TARGET"))
        n = len(trades)
        out[regime] = {
            "n": n,
            "wins": wins,
            "win_rate_pct": round(100 * wins / n, 1) if n else 0.0,
            "avg_r": round(sum(rr) / len(rr), 3) if rr else 0.0,
            "net_pnl": round(sum(usd), 2) if usd else 0.0,
            "total_r": round(sum(rr), 3) if rr else 0.0,
        }
    return out


def compute_time_bucket_stats(rows: list[dict]) -> dict:
    """Performance by session time bucket (prime/normal/degraded).

    prime = 19:00-00:15 UTC (first 30-90 min, strongest moves)
    normal = 13:30-19:00 UTC
    degraded = 00:15-00:45 UTC (last 30 min, weak trends)
    """
    by_bucket = defaultdict(list)
    for r in rows:
        bucket = r.get("session_bucket", "").strip() or "unknown"
        by_bucket[bucket].append(r)

    out = {}
    for bucket, trades in sorted(by_bucket.items()):
        rr = [_safe_float(x["result_r"]) for x in trades if x.get("result_r") not in ("", None)]
        usd = [_safe_float(x["result_usd"]) for x in trades if x.get("result_usd") not in ("", None)]
        wins = sum(1 for r in trades
                   if r.get("outcome", "").strip().upper() in ("W", "WIN", "TARGET"))
        n = len(trades)
        commissions = [_safe_float(x.get("commission_usd", 0)) for x in trades]
        time_in = [_safe_float(x.get("time_in_trade_min", 0)) for x in trades]
        out[bucket] = {
            "n": n,
            "wins": wins,
            "win_rate_pct": round(100 * wins / n, 1) if n else 0.0,
            "avg_r": round(sum(rr) / len(rr), 3) if rr else 0.0,
            "net_pnl": round(sum(usd), 2) if usd else 0.0,
            "total_commission": round(sum(commissions), 2),
            "avg_time_min": round(sum(time_in) / len(time_in), 1) if time_in else 0.0,
        }
    return out


def compute_commission_drag(rows: list[dict]) -> dict:
    """Commission analysis: total fees, fees as % of gross P&L, per-trade avg.

    Commission drag = total_commissions / |gross_wins|. High drag means
    fees are eating a significant portion of profits.
    """
    commissions = [_safe_float(r.get("commission_usd", 0)) for r in rows]
    usd = [_safe_float(r["result_usd"]) for r in rows if r.get("result_usd") not in ("", None)]
    wins_pnl = [u for u in usd if u > 0]
    losses_pnl = [u for u in usd if u <= 0]

    total_comm = sum(commissions)
    gross_wins = sum(wins_pnl)
    gross_losses = abs(sum(losses_pnl))
    net_pnl = sum(usd)

    n = len(rows)
    return {
        "total_commission": round(total_comm, 2),
        "avg_commission_per_trade": round(total_comm / n, 2) if n else 0.0,
        "gross_wins": round(gross_wins, 2),
        "gross_losses": round(gross_losses, 2),
        "net_pnl": round(net_pnl, 2),
        "commission_drag_pct": round(100 * total_comm / gross_wins, 1) if gross_wins > 0 else 0.0,
        "commission_drag_of_net_pct": round(100 * total_comm / abs(net_pnl), 1) if net_pnl != 0 else 0.0,
    }


def compute_streak_analysis(rows: list[dict]) -> dict:
    """Consecutive loss streak analysis and max drawdown."""
    max_loss_streak = 0
    current_streak = 0
    max_win_streak = 0
    current_win_streak = 0

    for r in rows:
        outcome = r.get("outcome", "").strip().upper()
        if outcome in ("L", "LOSS", "STOP"):
            current_streak += 1
            current_win_streak = 0
            max_loss_streak = max(max_loss_streak, current_streak)
        elif outcome in ("W", "WIN", "TARGET"):
            current_win_streak += 1
            current_streak = 0
            max_win_streak = max(max_win_streak, current_win_streak)
        else:
            current_streak = 0
            current_win_streak = 0

    # Max drawdown in R
    cum_r = 0.0
    peak_r = 0.0
    max_dd_r = 0.0
    for r in rows:
        cum_r += _safe_float(r.get("result_r", 0))
        peak_r = max(peak_r, cum_r)
        dd = peak_r - cum_r
        max_dd_r = max(max_dd_r, dd)

    return {
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
        "max_drawdown_r": round(max_dd_r, 3),
        "current_streak": current_streak,
    }


def compute_checklist_stats(date_filter: str = "") -> dict:
    """Read checklist_log.csv and compute pass/fail statistics.

    The checklist log is written by runner.py for every signal evaluation.
    """
    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "challenge", "checklist_log.csv")
    if not os.path.exists(log_path):
        return {"total": 0, "passed": 0, "blocked": 0, "pass_rate": 0.0,
                "block_reasons": {}, "by_symbol": {}}

    rows = []
    with open(log_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if date_filter and not r.get("ts", "").startswith(date_filter):
                continue
            rows.append(r)

    if not rows:
        return {"total": 0, "passed": 0, "blocked": 0, "pass_rate": 0.0,
                "block_reasons": {}, "by_symbol": {}}

    total = len(rows)
    passed = sum(1 for r in rows if r.get("passed") == "True")
    blocked = total - passed

    # Block reason breakdown
    block_reasons = defaultdict(int)
    for r in rows:
        if r.get("passed") == "False":
            reason = r.get("reason", "unknown")
            # Normalize: extract the check name
            if "position limit" in reason:
                block_reasons["position_limit"] += 1
            elif "already open" in reason:
                block_reasons["duplicate"] += 1
            elif "daily loss" in reason:
                block_reasons["equity_buffer"] += 1
            elif "below 1 share" in reason or "fees exceed" in reason:
                block_reasons["sizing"] += 1
            elif "stop-day" in reason:
                block_reasons["stop_day"] += 1
            elif "max" in reason and "trades" in reason:
                block_reasons["max_attempts"] += 1
            elif "losses today" in reason:
                block_reasons["max_losses"] += 1
            elif "flatten window" in reason:
                block_reasons["session_time"] += 1
            elif "S/R" in reason:
                block_reasons["sr_proximity"] += 1
            elif "quality" in reason:
                block_reasons["quality_score"] += 1
            else:
                block_reasons["other"] += 1

    # By symbol
    by_symbol = defaultdict(lambda: {"passed": 0, "blocked": 0})
    for r in rows:
        sym = r.get("symbol", "?")
        if r.get("passed") == "True":
            by_symbol[sym]["passed"] += 1
        else:
            by_symbol[sym]["blocked"] += 1

    return {
        "total": total,
        "passed": passed,
        "blocked": blocked,
        "pass_rate": round(100 * passed / total, 1) if total else 0.0,
        "block_reasons": dict(block_reasons),
        "by_symbol": dict(by_symbol),
    }


def generate_report(journal_path: str = DEFAULT_JOURNAL, date_filter: str = "") -> dict:
    """Generate the full discipline report.

    Args:
        journal_path: path to journal.csv
        date_filter: if set, only include trades on this date (YYYY-MM-DD)

    Returns dict with all report sections.
    """
    rows = journal_read(journal_path)
    if date_filter:
        rows = [r for r in rows if r.get("date") == date_filter]

    return {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "trade_count": len(rows),
        "adherence": compute_adherence(rows),
        "checklist": compute_checklist_stats(date_filter),
        "regime_breakdown": compute_regime_breakdown(rows),
        "time_bucket_stats": compute_time_bucket_stats(rows),
        "commission_drag": compute_commission_drag(rows),
        "streak": compute_streak_analysis(rows),
    }


def format_report(report: dict) -> str:
    """Format the discipline report as a human-readable string."""
    lines = []
    lines.append("=" * 60)
    lines.append("DAILY DISCIPLINE REPORT")
    lines.append(f"As of: {report.get('as_of', '?')}")
    lines.append(f"Trades: {report.get('trade_count', 0)}")
    lines.append("=" * 60)

    # --- Adherence ---
    a = report.get("adherence", {})
    lines.append("")
    lines.append("JOURNAL ADHERENCE")
    lines.append(f"  Complete records: {a.get('complete', 0)}/{a.get('total', 0)} "
                 f"({a.get('adherence_pct', 0):.0f}%)")
    lines.append(f"  By-plan trades:  {a.get('by_plan_pct', 0):.0f}%")
    if a.get("missing_fields"):
        lines.append(f"  Missing fields:  {a['missing_fields']}")

    # --- Checklist ---
    cl = report.get("checklist", {})
    if cl.get("total", 0) > 0:
        lines.append("")
        lines.append("CHECKLIST PASS/FAIL")
        lines.append(f"  Total signals:  {cl['total']}")
        lines.append(f"  Passed:         {cl['passed']}  ({cl['pass_rate']:.0f}%)")
        lines.append(f"  Blocked:        {cl['blocked']}")
        if cl.get("block_reasons"):
            lines.append(f"  Block reasons:")
            for reason, count in sorted(cl["block_reasons"].items(),
                                        key=lambda x: -x[1]):
                lines.append(f"    {reason:<20s} {count}")

    # --- Regime breakdown ---
    rb = report.get("regime_breakdown", {})
    if rb:
        lines.append("")
        lines.append("REGIME BREAKDOWN")
        lines.append(f"  {'Regime':<14s} {'N':>4s} {'WR%':>6s} {'AvgR':>7s} {'Net$':>9s}")
        lines.append(f"  {'-'*45}")
        for regime, stats in sorted(rb.items()):
            lines.append(f"  {regime:<14s} {stats['n']:>4d} {stats['win_rate_pct']:>5.0f}% "
                         f"{stats['avg_r']:>+6.3f} ${stats['net_pnl']:>+8.2f}")

    # --- Time bucket ---
    tb = report.get("time_bucket_stats", {})
    if tb:
        lines.append("")
        lines.append("TIME BUCKET STATS")
        lines.append(f"  {'Bucket':<12s} {'N':>4s} {'WR%':>6s} {'AvgR':>7s} {'Comm$':>7s} {'AvgMin':>7s}")
        lines.append(f"  {'-'*50}")
        for bucket, stats in sorted(tb.items()):
            lines.append(f"  {bucket:<12s} {stats['n']:>4d} {stats['win_rate_pct']:>5.0f}% "
                         f"{stats['avg_r']:>+6.3f} ${stats['total_commission']:>5.2f} "
                         f"{stats['avg_time_min']:>6.1f}")

    # --- Commission drag ---
    cd = report.get("commission_drag", {})
    lines.append("")
    lines.append("COMMISSION DRAG")
    lines.append(f"  Total fees:        ${cd.get('total_commission', 0):.2f}")
    lines.append(f"  Avg fee/trade:     ${cd.get('avg_commission_per_trade', 0):.2f}")
    lines.append(f"  Gross wins:        ${cd.get('gross_wins', 0):.2f}")
    lines.append(f"  Commission drag:   {cd.get('commission_drag_pct', 0):.1f}% of gross wins")
    lines.append(f"  Net P&L:           ${cd.get('net_pnl', 0):.2f}")

    # --- Streak ---
    st = report.get("streak", {})
    lines.append("")
    lines.append("STREAK ANALYSIS")
    lines.append(f"  Max loss streak:   {st.get('max_loss_streak', 0)}")
    lines.append(f"  Max win streak:    {st.get('max_win_streak', 0)}")
    lines.append(f"  Max drawdown:      {st.get('max_drawdown_r', 0):.3f}R")
    lines.append(f"  Current streak:    {st.get('current_streak', 0)}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
