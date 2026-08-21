# -*- coding: utf-8 -*-
"""Setup outcome tracker for the manual system (ТЗ §4.6/§7): records how each
alerted A/B setup actually resolved on live 1-minute candles and accumulates
per-grade statistics.

Exit model (the plan sent with each alert — data-driven, 2026-08-21, from a
411-setup / 24-week backtest):
  - the FULL position rides to the take-profit (+target_rr R, live 3.5R);
  - hard stop at -1R;
  - everything closed at the session end (19:55 UTC) if neither happened.
Partial fills at +1R and the breakeven move were removed: they capped winners
at 1.5R and converted runners into +0.5R scratches (avgR -0.021 vs +0.295
for full-position 3.5R). Resolution is decided from real candles after the
signal bar, so the journal is an honest, machine-measured record — not the
user's memory.

Data files (all under data/manual/):
  setup_outcomes.csv      — append-only journal of every resolved setup
  outcomes_resolved.json  — which alerts_sent keys are already resolved
  setup_stats.json        — cumulative aggregates by grade (A/B) + total
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time

DEFAULT_DAY_END = "19:55"
CSV_FIELDS = ["date", "symbol", "grade", "bias", "signal_utc",
              "entry", "stop", "target", "rr", "outcome", "r",
              "minutes", "resolved_utc"]

# outcome key -> human label
OUTCOME_LABELS = {
    "stop":        "СТОП (−1R)",
    "target":      "ТЕЙК (по плану)",
    "eod":         "закрытие EOD по плану",
    # legacy keys (pre-2026-08-21 plan with the 50%@1R -> BE -> 2R model)
    "r1_be":       "1R → BE (+0.5R)",
    "r1_r2":       "1R → 2R (+1.5R)",
    "eod_after_r1": "1R, остаток закрыт на EOD",
}


def _utc(ts) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


def simulate_outcome(signal_ts, entry, stop, target, bias, candles,
                     now_ts=None, day_end=DEFAULT_DAY_END):
    """Simulate the planned exit on real candles.

    Plan (2026-08-21): the FULL position rides to the take-profit, hard stop
    at -1R, otherwise everything closes at the session end (19:55 UTC).

    Returns (outcome, r, minutes) once the setup is decided, or
    (None, None, None) while the session of the setup's day is still running
    (stop/target not hit yet and day_end not reached).

    outcomes: stop | target | eod
    """
    sig = _utc(signal_ts)
    sig_date = sig.date()
    h, m = map(int, day_end.split(":"))
    day_end_dt = dt.datetime.combine(sig_date, dt.time(h, m), tzinfo=dt.timezone.utc)
    day_end_ts = int(day_end_dt.timestamp())
    now_ts = int(time.time()) if now_ts is None else int(now_ts)

    long = bias == "long"
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        return None, None, None
    rr = abs(target - entry) / risk if target else 0.0

    bars = [c for c in candles
            if c["time"] > signal_ts and c["time"] <= day_end_ts
            and _utc(c["time"]).date() == sig_date]

    for c in bars:
        t = _utc(c["time"])
        mins = (t - sig).total_seconds() / 60
        if long:
            if c["low"] <= stop:              # stop wins intrabar (conservative)
                return "stop", -1.0, mins
            if target and c["high"] >= target:
                return "target", rr, mins
        else:
            if c["high"] >= stop:
                return "stop", -1.0, mins
            if target and c["low"] <= target:
                return "target", rr, mins

    if now_ts < day_end_ts:
        return None, None, None          # market still open — pending

    eod_close = bars[-1]["close"] if bars else entry
    r_eod = (eod_close - entry) / risk if long else (entry - eod_close) / risk
    mins_total = (day_end_dt - sig).total_seconds() / 60
    return "eod", round(r_eod, 3), round(mins_total, 1)


# ---------------------------------------------------------------------------
# Journal (CSV) + resolved-state + stats
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
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def read_journal(path) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return [r for r in rows if r.get("date")]
    except Exception:
        return []


def compute_stats(rows: list) -> dict:
    """Cumulative per-grade aggregates. Rows without a numeric `r` are skipped."""
    out = {"as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    for label, grade in (("A", "A"), ("B", "B"), ("total", None)):
        rs = rows if grade is None else [r for r in rows if r.get("grade") == grade]
        vals = []
        for r in rs:
            try:
                vals.append(float(r["r"]))
            except (KeyError, TypeError, ValueError):
                continue
        n = len(vals)
        if n == 0:
            out[label] = {"n": 0, "wins": 0, "losses": 0, "flat": 0,
                          "sum_r": 0.0, "avg_r": None, "win_rate_pct": None}
            continue
        wins = sum(1 for v in vals if v > 0)
        losses = sum(1 for v in vals if v < 0)
        out[label] = {
            "n": n, "wins": wins, "losses": losses, "flat": n - wins - losses,
            "sum_r": round(sum(vals), 3), "avg_r": round(sum(vals) / n, 3),
            "win_rate_pct": round(100 * wins / n, 1),
        }
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


def _fmt_avg(v) -> str:
    return "—" if v is None else f"{v:+.2f}"


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------

def format_resolution(row: dict, stats: dict) -> str:
    label = OUTCOME_LABELS.get(row.get("outcome"), str(row.get("outcome")))
    mins = row.get("minutes")
    m = ""
    if mins not in ("", None):
        try:
            m = f" (через {float(mins):.0f} мин)"
        except (TypeError, ValueError):
            m = ""
    a, b = stats.get("A", {}), stats.get("B", {})
    return (
        f"📊 ИСХОД {row.get('date')} {row.get('symbol')} — {label} "
        f"R{float(row.get('r', 0)):+.2f}{m}\n"
        f"Класс {row.get('grade', '')} | {str(row.get('bias', '')).upper()} | "
        f"сигнал {row.get('signal_utc', '')}\n"
        f"Вход {row.get('entry')} | Стоп {row.get('stop')} | Тейк {row.get('target')}\n"
        f"Накоплено: A {a.get('n', 0)} сд., avgR {_fmt_avg(a.get('avg_r'))} | "
        f"B {b.get('n', 0)} сд., avgR {_fmt_avg(b.get('avg_r'))}"
    )


def format_stats_summary(stats: dict) -> str:
    def line(d):
        if not d or d.get("n", 0) == 0:
            return "сделок ещё нет"
        return (f"{d['n']} сд. | побед {d['wins']} | убытков {d['losses']} | "
                f"flat {d['flat']} | сумма R {d['sum_r']:+.2f} | "
                f"avgR {d['avg_r']:+.2f} | WR {d['win_rate_pct']}%")
    as_of = (stats.get("as_of") or "")[:16]
    return (
        f"📈 Накопительная статистика сетапов (по {as_of} UTC)\n"
        f"A: {line(stats.get('A'))}\n"
        f"B: {line(stats.get('B'))}\n"
        f"Всего: {line(stats.get('total'))}"
    )
