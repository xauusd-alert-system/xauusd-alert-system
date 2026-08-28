# -*- coding: utf-8 -*-
"""Pair signal outcome tracker (аналогично现货 outcomes.py).

Monitors z-score after a pair signal is sent and resolves the outcome:
  - exit_z: z crossed 0.0 (mean-reversion target hit)
  - stop_z: |z| > 3.0σ (stop hit)
  - timeout: 2×HL bars elapsed without exit
  - end_of_data: data ended before resolution

Stores open signals in pair_alerts_sent.json (extended with entry_z, direction,
half_life_days, signal_bar_index). Resolves by re-analyzing the pair and checking
current z against entry conditions.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pairs_analysis import PairAnalyzer
from pairs_analysis import load_config as load_pairs_config

PAIR_SENT_FILE = os.path.join(ROOT, "data", "manual", "pair_alerts_sent.json")
PAIR_RESOLVED_FILE = os.path.join(ROOT, "data", "manual", "pair_outcomes_resolved.json")
PAIR_JOURNAL_CSV = os.path.join(ROOT, "data", "manual", "pair_journal.csv")

EXIT_Z = 0.0
STOP_Z = 3.0


def load_pair_sent() -> dict:
    if os.path.exists(PAIR_SENT_FILE):
        try:
            with open(PAIR_SENT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_pair_sent(sent: dict) -> None:
    os.makedirs(os.path.dirname(PAIR_SENT_FILE), exist_ok=True)
    with open(PAIR_SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=2, ensure_ascii=False)


def load_pair_resolved() -> dict:
    if os.path.exists(PAIR_RESOLVED_FILE):
        try:
            with open(PAIR_RESOLVED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_pair_resolved(resolved: dict) -> None:
    os.makedirs(os.path.dirname(PAIR_RESOLVED_FILE), exist_ok=True)
    with open(PAIR_RESOLVED_FILE, "w", encoding="utf-8") as f:
        json.dump(resolved, f, indent=2, ensure_ascii=False)


def _get_current_z(pair_name: str, tf: str = "D1") -> tuple[float, float] | None:
    """Re-analyze a pair and return (current_z, half_life_days).
    Returns None on error."""
    try:
        cfg = load_pairs_config()
        analysis = cfg.get("analysis", {})
        pair_cfg = next((p for p in cfg.get("pairs", []) if p["name"] == pair_name), None)
        if not pair_cfg:
            return None
        pa = PairAnalyzer(pair_cfg, analysis)
        m = pa.analyze(tf)
        z = float(m.zscore.dropna().iloc[-1]) if len(m.zscore.dropna()) else 0.0
        hl = m.half_life_days
        return z, hl
    except Exception as e:
        print(f"pair_outcome: {pair_name} analyze error: {e}", file=sys.stderr)
        return None


def resolve_pair_outcomes(tf: str = "D1") -> int:
    """Check all open pair signals and resolve completed ones.
    Returns the number of newly resolved outcomes."""
    sent = load_pair_sent()
    resolved = load_pair_resolved()
    now = dt.datetime.now(dt.UTC)
    changed = 0

    for key, rec in sorted(sent.items()):
        if key in resolved or not isinstance(rec, dict):
            continue

        pair_name = rec.get("pair_name", key)
        direction = rec.get("direction", "")
        entry_z = float(rec.get("entry_z", 0))
        signal_time = rec.get("signal_time", rec.get("sent_at", ""))
        hl_days = float(rec.get("half_life_days", 2.0))
        ensemble_line = rec.get("ensemble_line", "")

        if not direction or not signal_time:
            continue

        # Parse signal time
        try:
            sig_dt = dt.datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        # Get current z
        result = _get_current_z(pair_name, tf)
        if result is None:
            continue
        z_cur, hl_cur = result
        if not np.isfinite(z_cur):
            continue

        # Time held (in days, approximate)
        held_days = (now - sig_dt).total_seconds() / 86400

        # Determine outcome
        outcome = None
        r = 0.0

        if direction == "long":
            # Long spread: z was < -entry_z, target is z >= EXIT_Z (0)
            if z_cur >= EXIT_Z:
                outcome = "exit_z"
                r = (z_cur - entry_z) / (STOP_Z + entry_z) if (STOP_Z + entry_z) != 0 else 0
            elif z_cur <= -STOP_Z:
                outcome = "stop_z"
                r = -1.0
        elif direction == "short":
            # Short spread: z was > +entry_z, target is z <= EXIT_Z (0)
            if z_cur <= EXIT_Z:
                outcome = "exit_z"
                r = (entry_z - z_cur) / (STOP_Z - entry_z) if (STOP_Z - entry_z) != 0 else 0
            elif z_cur >= STOP_Z:
                outcome = "stop_z"
                r = -1.0

        # Timeout: 2×HL days
        if outcome is None and np.isfinite(hl_days) and hl_days > 0:
            if held_days >= 2.0 * hl_days:
                outcome = "timeout"
                if direction == "long":
                    r = (z_cur - entry_z) / (STOP_Z + entry_z) if (STOP_Z + entry_z) != 0 else 0
                else:
                    r = (entry_z - z_cur) / (STOP_Z - entry_z) if (STOP_Z - entry_z) != 0 else 0

        if outcome is None:
            continue  # still open

        r = round(float(r), 3)
        resolved[key] = {
            "outcome": outcome,
            "r": r,
            "z_on_exit": round(z_cur, 3),
            "held_days": round(held_days, 1),
            "resolved_utc": now.isoformat(timespec="seconds"),
        }
        save_pair_resolved(resolved)
        changed += 1

        # Log to journal
        try:
            from pairs_analysis.integrations import log_pair_trade
            log_pair_trade(
                PAIR_JOURNAL_CSV,
                date=sig_dt.strftime("%Y-%m-%d"),
                time_str=sig_dt.strftime("%H:%M"),
                pair=pair_name,
                direction=direction,
                spread_direction=direction,
                entry_z=entry_z,
                exit_z=round(z_cur, 3),
                exit_reason=outcome,
                r=r,
                bars_held=int(held_days * 24) if tf == "H1" else int(held_days),
                beta=rec.get("beta", 0),
                hedge_mode="dollar_neutral",
                risk_usd=rec.get("risk_usd", 0),
                p1_symbol=pair_name.split("/")[0] + "USD" if "/" in pair_name else pair_name,
                p1_contracts=0,
                p2_symbol=pair_name.split("/")[1] + "USD" if "/" in pair_name else "",
                p2_contracts=0,
                adf_p=rec.get("adf_p", 0),
                half_life_days=hl_days,
                hurst=rec.get("hurst", 0),
                regime=rec.get("regime", ""),
                ensemble_direction=rec.get("ensemble_direction", ""),
                ensemble_confidence=float(rec.get("ensemble_confidence", 0)),
                z_on_exit=round(z_cur, 3),
            )
        except Exception as e:
            print(f"pair_outcome: journal log error: {e}", file=sys.stderr)

        print(f"{now:%H:%M:%S} UTC: pair outcome {pair_name}: {outcome} R{r:+.2f} "
              f"(z {entry_z:+.2f} -> {z_cur:+.2f}, {held_days:.1f}d)", file=sys.stderr)

    return changed


def format_pair_resolution(pair_name: str, rec: dict, outcome: dict,
                           ensemble_line: str = "") -> str:
    """Format a Telegram message for a resolved pair trade."""
    direction = rec.get("direction", "")
    entry_z = float(rec.get("entry_z", 0))
    z_exit = outcome.get("z_on_exit", 0)
    r = outcome.get("r", 0)
    reason = outcome.get("outcome", "")
    held = outcome.get("held_days", 0)

    reason_ru = {
        "exit_z": "Тейк (z → 0)",
        "stop_z": "Стоп (|z| > 3σ)",
        "timeout": "Таймаут (2×HL)",
    }.get(reason, reason)

    emoji = "✅" if r > 0 else "❌" if r < 0 else "➡️"

    lines = [
        f"{emoji} ИСХОД {pair_name} — {reason_ru}",
        f"R{r:+.2f} | z: {entry_z:+.2f} → {z_exit:+.2f} | {held:.1f} дн",
        f"Направление: {direction.upper()}",
    ]
    if ensemble_line:
        lines.append(f"Ensemble: {ensemble_line}")
    return "\n".join(lines)
