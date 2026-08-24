# -*- coding: utf-8 -*-
"""Auto-calibration for quality thresholds.

Reads alerts_sent.json + outcomes_resolved.json and recalibrates
quality thresholds per setup type. Should run weekly (or when enough
new data accumulates).

Merges: alerts_sent[date:type:symbol].quality_score with
        outcomes_resolved[date:type:symbol].r

Then sweeps thresholds 0-100 to find optimal per-type threshold.
Saves results to quality_thresholds.json.
"""
from __future__ import annotations

import json, os, sys, datetime as dt
from collections import defaultdict
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT, "data", "manual")
THRESHOLDS_PATH = os.path.join(
    ROOT, "challenge", "manual", "quality_thresholds.json"
)

SETUP_TYPES = ("impulse", "gap_fade", "opening_drive")


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def collect_live_data() -> dict:
    """Returns {setup_type: [(quality_score, r_outcome), ...]} from live data."""
    alerts = _load_json(os.path.join(DATA_DIR, "alerts_sent.json"))
    resolved = _load_json(os.path.join(DATA_DIR, "outcomes_resolved.json"))

    by_type = defaultdict(list)

    # Cross-reference: alerts_sent has quality_score, resolved has r
    for key, alert in alerts.items():
        if not isinstance(alert, dict):
            continue
        if "quality_score" not in alert:
            continue  # old alert, no quality data
        if key not in resolved:
            continue  # not resolved yet

        setup_type = alert.get("setup_type", "")
        if setup_type not in SETUP_TYPES:
            # Try to parse from key: date:type:symbol
            parts = key.split(":", 2)
            if len(parts) == 3:
                setup_type = parts[1]
        if setup_type not in SETUP_TYPES:
            continue

        qs = alert["quality_score"]
        r_val = resolved[key].get("r", 0)

        by_type[setup_type].append((qs, r_val))

    return dict(by_type)


def find_optimal_threshold(pairs: list, min_trades: int = 5) -> int:
    """Find quality threshold that maximizes avgR.
    
    Args:
        pairs: list of (quality_score, r_value) tuples
        min_trades: minimum number of trades required at threshold
    
    Returns: best threshold (0-100)
    """
    if len(pairs) < min_trades:
        return 0  # not enough data

    best_threshold = 0
    best_avg_r = -999.0

    for threshold in range(0, 101, 5):
        filtered = [r for qs, r in pairs if qs >= threshold]
        if len(filtered) < min_trades:
            continue
        avg_r = sum(filtered) / len(filtered)
        if avg_r > best_avg_r:
            best_avg_r = avg_r
            best_threshold = threshold

    return best_threshold


def recalibrate() -> dict:
    """Main recalibration. Returns the new threshold dict."""
    data = collect_live_data()

    new_thresholds = {
        "calibrated_at": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "min_samples_per_type": 10,
    }

    print("--- Quality Auto-Calibration ---")
    total_usable = 0

    for stype in SETUP_TYPES:
        pairs = data.get(stype, [])
        n = len(pairs)
        usable = n >= new_thresholds["min_samples_per_type"]

        if pairs:
            all_r = [r for _, r in pairs]
            avg_r = sum(all_r) / n
            wins = sum(1 for r in all_r if r > 0)
            print(f"  {stype}: {n} resolved trades, WR={100*wins/n:.0f}%, "
                  f"avgR={avg_r:+.3f}")

        if usable:
            best = find_optimal_threshold(pairs)
            filtered = [(qs, r) for qs, r in pairs if qs >= best]
            f_avg = sum(r for _, r in filtered) / len(filtered) if filtered else 0
            new_thresholds[stype] = best
            print(f"    -> optimal threshold: {best}, filtered avgR: {f_avg:+.3f} "
                  f"(from {len(filtered)}/{n} trades)")
        else:
            # Keep existing threshold
            old = _load_json(THRESHOLDS_PATH)
            new_thresholds[stype] = old.get(stype, 0)
            print(f"    -> keeping existing threshold: {new_thresholds[stype]} "
                  f"(not enough data)")

        total_usable += 1 if usable else 0

    print(f"\n  Usable types: {total_usable}/{len(SETUP_TYPES)}")
    return new_thresholds


def save_and_reload(thresholds: dict) -> dict:
    """Save thresholds and reload quality_gate module cache."""
    _save_json(THRESHOLDS_PATH, thresholds)
    print(f"  Saved to {THRESHOLDS_PATH}")

    # Force reload of quality_gate module
    try:
        from challenge.manual import quality_gate
        # Re-import to refresh
        import importlib
        importlib.reload(quality_gate)
    except Exception:
        pass

    return thresholds


def main() -> int:
    thresholds = recalibrate()
    save_and_reload(thresholds)

    print(f"\nCurrent thresholds:")
    for stype in SETUP_TYPES:
        print(f"  {stype}: {thresholds[stype]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())