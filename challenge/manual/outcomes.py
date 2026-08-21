# -*- coding: utf-8 -*-
"""Analysis-only outcome accounting for the US Stocks Headliners manual workflow.

The tracker evaluates the *documented plan* on candles after an alert. It never
observes a terminal, submits an order or claims to represent an actual fill.
Actual manual fills, fees and deviations belong in ``journal.py``.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time

DEFAULT_DAY_END_LOCAL = "00:50"
DEFAULT_UTC_OFFSET_MINUTES = 240
DEFAULT_FIRST_TARGET_R = 1.0
DEFAULT_FIRST_TARGET_FRACTION = 0.50
DEFAULT_FINAL_TARGET_R = 2.0
CSV_FIELDS = ["date", "symbol", "grade", "bias", "signal_utc",
              "entry", "stop", "target", "rr", "outcome", "r",
              "minutes", "resolved_utc"]

OUTCOME_LABELS = {
    "stop": "СТОП (−1R)",
    "r1_be": "50% на 1R, остаток BE (+0.5R)",
    "r1_r2": "50% на 1R, остаток на 2R (+1.5R)",
    "eod": "ручное закрытие до конца сессии",
    "eod_after_r1": "50% на 1R, остаток закрыт перед концом сессии",
}


def _utc(ts: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


def _session_end_ts(signal_ts: int, day_end_local: str,
                    utc_offset_minutes: int) -> int:
    """Convert a local end-of-session time to UTC, handling midnight crossover."""
    offset = dt.timezone(dt.timedelta(minutes=utc_offset_minutes))
    signal_local = _utc(signal_ts).astimezone(offset)
    h, m = map(int, day_end_local.split(":"))
    end_local = dt.datetime.combine(signal_local.date(), dt.time(h, m), tzinfo=offset)
    if end_local <= signal_local:
        end_local += dt.timedelta(days=1)
    return int(end_local.astimezone(dt.timezone.utc).timestamp())


def _eod_r(entry: float, close: float, risk: float, long: bool) -> float:
    raw = (close - entry) / risk if long else (entry - close) / risk
    return round(raw, 3)


def simulate_outcome(signal_ts, entry, stop, target, bias, candles,
                     now_ts=None, day_end_local: str = DEFAULT_DAY_END_LOCAL,
                     utc_offset_minutes: int = DEFAULT_UTC_OFFSET_MINUTES,
                     first_target_r: float = DEFAULT_FIRST_TARGET_R,
                     first_target_fraction: float = DEFAULT_FIRST_TARGET_FRACTION,
                     final_target_r: float = DEFAULT_FINAL_TARGET_R):
    """Simulate the versioned manual exit plan on subsequent one-minute candles.

    The plan is: full stop at -1R; close ``first_target_fraction`` at 1R; move
    the remainder to break-even; close the remainder at ``final_target_r`` or
    manually before the configured session end. Where one candle crosses two
    levels, the less favourable valid outcome is used to avoid optimistic bias.
    """
    if bias not in ("long", "short"):
        return None, None, None
    if not 0 < first_target_fraction < 1:
        raise ValueError("first_target_fraction must be between zero and one")
    sig = _utc(signal_ts)
    long = bias == "long"
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        return None, None, None

    first_target = entry + first_target_r * risk if long else entry - first_target_r * risk
    final_target = entry + final_target_r * risk if long else entry - final_target_r * risk
    # Retain caller-provided target only when it matches/extends the specified plan.
    if target:
        final_target = target
        final_target_r = abs(final_target - entry) / risk

    day_end_ts = _session_end_ts(int(signal_ts), day_end_local, utc_offset_minutes)
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    bars = [c for c in candles if int(c["time"]) > int(signal_ts) and int(c["time"]) <= day_end_ts]
    first_taken = False

    for c in bars:
        mins = round((_utc(c["time"]) - sig).total_seconds() / 60.0, 1)
        high, low = float(c["high"]), float(c["low"])
        if long:
            stop_hit = low <= (entry if first_taken else stop)
            first_hit = high >= first_target
            final_hit = high >= final_target
        else:
            stop_hit = high >= (entry if first_taken else stop)
            first_hit = low <= first_target
            final_hit = low <= final_target

        if not first_taken:
            # When a 1m bar both touches stop and first target, treat stop as first.
            if stop_hit:
                return "stop", -1.0, mins
            if first_hit:
                first_taken = True
                # The same bar could also cross the final target; conservatively do
                # not grant the runner its target without a subsequent bar.
                continue
        else:
            # Once half is realised at +1R, a break-even stop protects the runner.
            if stop_hit:
                return "r1_be", round(first_target_fraction * first_target_r, 3), mins
            if final_hit:
                total_r = first_target_fraction * first_target_r + (1 - first_target_fraction) * final_target_r
                return "r1_r2", round(total_r, 3), mins

    if now_ts < day_end_ts:
        return None, None, None
    eod_close = float(bars[-1]["close"]) if bars else entry
    if first_taken:
        total_r = first_target_fraction * first_target_r + (1 - first_target_fraction) * _eod_r(entry, eod_close, risk, long)
        return "eod_after_r1", round(total_r, 3), round((day_end_ts - signal_ts) / 60.0, 1)
    return "eod", _eod_r(entry, eod_close, risk, long), round((day_end_ts - signal_ts) / 60.0, 1)


# ---------------------------------------------------------------------------
# Journal of theoretical setup outcomes (separate from actual manual trades)
# ---------------------------------------------------------------------------

def load_resolved(path) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_resolved(path, resolved: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(resolved, f, indent=2, ensure_ascii=False)


def append_journal(path, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def read_journal(path) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("date")]
    except Exception:
        return []


def compute_stats(rows: list) -> dict:
    out = {"as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    for label, grade in (("A", "A"), ("B", "B"), ("total", None)):
        subset = rows if grade is None else [r for r in rows if r.get("grade") == grade]
        vals = []
        for row in subset:
            try:
                vals.append(float(row["r"]))
            except (KeyError, TypeError, ValueError):
                continue
        n = len(vals)
        if not n:
            out[label] = {"n": 0, "wins": 0, "losses": 0, "flat": 0,
                          "sum_r": 0.0, "avg_r": None, "win_rate_pct": None}
            continue
        wins = sum(v > 0 for v in vals)
        losses = sum(v < 0 for v in vals)
        out[label] = {"n": n, "wins": wins, "losses": losses,
                      "flat": n - wins - losses, "sum_r": round(sum(vals), 3),
                      "avg_r": round(sum(vals) / n, 3),
                      "win_rate_pct": round(100 * wins / n, 1)}
    return out


def save_stats(path, stats: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)


def load_stats(path) -> dict | None:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def format_resolution(row: dict, stats: dict) -> str:
    label = OUTCOME_LABELS.get(row.get("outcome"), str(row.get("outcome")))
    return (
        f"ИСХОД {row.get('date')} {row.get('symbol')} — {label} R{float(row.get('r', 0)):+.2f}\n"
        f"Класс {row.get('grade', '')} | {str(row.get('bias', '')).upper()} | "
        f"вход {row.get('entry')} | стоп {row.get('stop')} | тейк {row.get('target')}\n"
        f"Накоплено: A {stats.get('A', {}).get('n', 0)} | B {stats.get('B', {}).get('n', 0)}"
    )


def format_stats_summary(stats: dict) -> str:
    def line(data):
        if not data or data.get("n", 0) == 0:
            return "сделок ещё нет"
        return (f"{data['n']} сд. | побед {data['wins']} | убытков {data['losses']} | "
                f"avgR {data['avg_r']:+.2f} | WR {data['win_rate_pct']}%")
    return f"Статистика сетапов\nA: {line(stats.get('A'))}\nB: {line(stats.get('B'))}\nВсего: {line(stats.get('total'))}"
