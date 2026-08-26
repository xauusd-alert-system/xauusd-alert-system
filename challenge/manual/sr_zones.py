# -*- coding: utf-8 -*-
"""Support / Resistance zone detection for the prop-challenge scanner.

RESEARCH 2026-08-22 (us_stocks audit §5.2):
- S/R should be zones, not lines (Tengelin & Sopasakis clustering, StockCharts)
- Key zones: previous-day high/low/close, premarket high/low, multi-touch swings
- Entry should NOT be within `buffer` of a zone — either skip or place stop beyond
- Zones are filtered by touch count (≥2 touches = strong zone)

Input: 1-minute candles for the current trading day + N prior days.
Output: list of S/R zones with strength classification.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
import yaml


# Manual-system config (UTEx/stock scanner session windows). Defaults below
# match the original NYSE constants (premarket 09:00-13:30, session 13:30-
# 19:55 UTC); override at runtime via challenge/manual/manual_config.yaml:
#   premarket_start_utc / premarket_end_utc / session_start_utc / session_end_utc
DEFAULT_PREMARKET_START_SEC = 9 * 3600
DEFAULT_PREMARKET_END_SEC = 13 * 3600 + 30 * 60
DEFAULT_SESSION_START_SEC = 13 * 3600 + 30 * 60
DEFAULT_SESSION_END_SEC = 19 * 3600 + 55 * 60

_MANUAL_CFG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manual_config.yaml")


def _hm_to_sec(hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return h * 3600 + m * 60


def _load_window_config() -> tuple[int, int, int, int]:
    """Load session/premarket windows from manual_config.yaml (UTC seconds).

    Falls back to the historical NYSE defaults when the file or a key is
    missing, so callers/tests that never touch config keep working unchanged.
    Returns (premarket_start, premarket_end, session_start, session_end).
    """
    pm_start, pm_end = DEFAULT_PREMARKET_START_SEC, DEFAULT_PREMARKET_END_SEC
    sess_start, sess_end = DEFAULT_SESSION_START_SEC, DEFAULT_SESSION_END_SEC
    try:
        if os.path.isfile(_MANUAL_CFG_PATH):
            with open(_MANUAL_CFG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            pm_start = _hm_to_sec(str(cfg.get("premarket_start_utc", "09:00")))
            pm_end = _hm_to_sec(str(cfg.get("premarket_end_utc", "13:30")))
            sess_start = _hm_to_sec(str(cfg.get("session_start_utc", "13:30")))
            sess_end = _hm_to_sec(str(cfg.get("session_end_utc", "19:55")))
    except Exception:  # pragma: no cover - config-dependent; never fat-
        pass          #                       al: keep defaults on parse errors
    return pm_start, pm_end, sess_start, sess_end


PREMARKET_START_SEC, PREMARKET_END_SEC, SESSION_START_SEC, SESSION_END_SEC = (
    _load_window_config())


@dataclass
class SRZone:
    """A support or resistance zone."""
    price: float            # center of zone
    zone_type: str          # prev_high | prev_low | prev_close | premarket_high | premarket_low | swing_high | swing_low
    direction: str          # resistance | support
    strength: int           # number of touches (1 = single level, 2+ = cluster)
    confidence: float       # 0-1, based on touches + recency
    source_date: str = ""   # which day this came from

    def distance_from(self, price: float) -> float:
        return abs(self.price - price)


def _utc_sec(ts: int) -> int:
    """Extract seconds-since-midnight UTC from a timestamp."""
    utc = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return utc.hour * 3600 + utc.minute * 60 + utc.second


def _utc_date(ts: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()


def _session_bars(candles, date: dt.date) -> list:
    """Extract 1-min bars within the NYSE session for a given date."""
    return [c for c in candles
            if _utc_date(c["time"]) == date
            and SESSION_START_SEC <= _utc_sec(c["time"]) <= SESSION_END_SEC]


def _premarket_bars(candles, date: dt.date) -> list:
    """Extract 1-min bars within premarket (09:00-13:30 UTC) for a given date."""
    return [c for c in candles
            if _utc_date(c["time"]) == date
            and PREMARKET_START_SEC <= _utc_sec(c["time"]) <= PREMARKET_END_SEC]


def _prior_day_data(candles: list, current_date: dt.date, lookback_days: int = 5) -> list[dt.date]:
    """Return list of trading dates before current_date, up to lookback_days."""
    all_dates = sorted(set(_utc_date(c["time"]) for c in candles
                           if _utc_date(c["time"]) < current_date
                           and _utc_date(c["time"]).weekday() < 5))
    return all_dates[-lookback_days:]


def _swing_points(candles: list, lookback: int = 20) -> list[tuple[float, str]]:
    """Detect local swing highs/lows from 5-min bars using a simple pivot method.
    Returns list of (price, 'swing_high'|'swing_low')."""
    if len(candles) < lookback + 2:
        return []
    # Resample to 5-min if needed (assume input is already 5-min or finer)
    bars = candles  # caller should pass 5-min bars
    swings = []
    half = lookback // 2
    for i in range(half, len(bars) - half):
        # Swing high: highest high in the window
        window_highs = [bars[j]["high"] for j in range(i - half, i + half + 1)]
        if bars[i]["high"] == max(window_highs):
            swings.append((bars[i]["high"], "swing_high"))
        # Swing low: lowest low in the window
        window_lows = [bars[j]["low"] for j in range(i - half, i + half + 1)]
        if bars[i]["low"] == min(window_lows):
            swings.append((bars[i]["low"], "swing_low"))
    # Deduplicate nearby swings (within 0.1% of each other)
    if not swings:
        return []
    swings.sort(key=lambda x: x[0])
    deduped = [swings[0]]
    for price, kind in swings[1:]:
        if abs(price - deduped[-1][0]) / max(1, deduped[-1][0]) > 0.001:
            deduped.append((price, kind))
    return deduped


def _cluster_zones(levels: list[tuple[float, str, str]], tolerance_pct: float = 0.002) -> list[SRZone]:
    """Cluster nearby price levels into zones. Each cluster becomes one SRZone.
    tolerance_pct: two levels within this % of each other merge into one zone."""
    if not levels:
        return []
    levels.sort(key=lambda x: x[0])
    zones = []
    current_cluster = [levels[0]]
    for item in levels[1:]:
        ref_price = current_cluster[0][0]
        if abs(item[0] - ref_price) / max(1, ref_price) <= tolerance_pct:
            current_cluster.append(item)
        else:
            zones.append(_make_zone(current_cluster))
            current_cluster = [item]
    zones.append(_make_zone(current_cluster))
    return zones


def _cluster_zones_with_direction(levels: list[tuple[float, str, str, str]],
                                 tolerance_pct: float = 0.002) -> list[SRZone]:
    """Cluster with pre-assigned directions.
    levels: (price, zone_type, source_date, direction)"""
    if not levels:
        return []
    levels.sort(key=lambda x: x[0])
    zones = []
    current_cluster = [levels[0]]
    for item in levels[1:]:
        ref_price = current_cluster[0][0]
        if abs(item[0] - ref_price) / max(1, ref_price) <= tolerance_pct:
            current_cluster.append(item)
        else:
            zones.append(_make_zone_with_direction(current_cluster))
            current_cluster = [item]
    zones.append(_make_zone_with_direction(current_cluster))
    return zones


def _make_zone(cluster: list[tuple[float, str, str]]) -> SRZone:
    """Create an SRZone from a cluster of nearby levels.
    cluster items: (price, zone_type, source_date)"""
    avg_price = sum(lv[0] for lv in cluster) / len(cluster)
    kinds = [lv[1] for lv in cluster]
    sources = [lv[2] for lv in cluster]
    touches = len(cluster)
    confidence = min(1.0, 0.3 + 0.2 * (touches - 1))
    if any(s in ("premarket_high", "premarket_low") for s in sources):
        confidence = min(1.0, confidence + 0.15)
    if any(s in ("prev_high", "prev_low", "prev_close") for s in sources):
        confidence = min(1.0, confidence + 0.10)
    # Direction: high -> resistance, low -> support
    resistance_types = sum(1 for k in kinds if "high" in k)
    support_types = sum(1 for k in kinds if "low" in k)
    if resistance_types > support_types:
        direction = "resistance"
    elif support_types > resistance_types:
        direction = "support"
    else:
        direction = "resistance" if any("high" in s for s in sources) else "support"
    source_date = sources[0] if sources else ""
    return SRZone(price=round(avg_price, 4), zone_type=f"cluster_{direction}",
                  direction=direction, strength=touches,
                  confidence=round(confidence, 2), source_date=source_date)


def _make_zone_with_direction(cluster: list[tuple[float, str, str, str]]) -> SRZone:
    """Create SRZone from cluster with pre-assigned directions.
    cluster items: (price, zone_type, source_date, direction)"""
    avg_price = sum(lv[0] for lv in cluster) / len(cluster)
    directions = [lv[3] for lv in cluster]
    touches = len(cluster)
    confidence = min(1.0, 0.3 + 0.2 * (touches - 1))
    direction = max(set(directions), key=directions.count)
    source_date = cluster[0][2]
    return SRZone(price=round(avg_price, 4), zone_type=f"cluster_{direction}",
                  direction=direction, strength=touches,
                  confidence=round(confidence, 2), source_date=source_date)


def detect_sr_zones(candles: list, current_date: dt.date,
                    lookback_days: int = 5) -> list[SRZone]:
    """Detect S/R zones from candle data.

    Sources (per us_stocks audit §5.2):
    1. Previous day high/low/close — key intraday S/R
    2. Premarket high/low — opening context
    3. Multi-touch swing points from recent sessions

    Returns sorted list of zones (closest to price first should be done by caller).
    """
    zones = []
    prior_dates = _prior_day_data(candles, current_date, lookback_days)

    # --- 1. Previous day levels ---
    for pd in prior_dates:
        pd_bars = _session_bars(candles, pd)
        if not pd_bars:
            continue
        pd_high = max(b["high"] for b in pd_bars)
        pd_low = min(b["low"] for b in pd_bars)
        pd_close = pd_bars[-1]["close"]
        pd_date = str(pd)

        zones.append(SRZone(pd_high, "prev_high", "resistance", 1, 0.5, pd_date))
        zones.append(SRZone(pd_low, "prev_low", "support", 1, 0.5, pd_date))
        zones.append(SRZone(pd_close, "prev_close", "resistance" if pd_close > (pd_high + pd_low) / 2 else "support",
                            1, 0.4, pd_date))

    # --- 2. Premarket levels for today ---
    pm_bars = _premarket_bars(candles, current_date)
    if pm_bars:
        pm_high = max(b["high"] for b in pm_bars)
        pm_low = min(b["low"] for b in pm_bars)
        today_str = str(current_date)

        zones.append(SRZone(pm_high, "premarket_high", "resistance", 1, 0.6, today_str))
        zones.append(SRZone(pm_low, "premarket_low", "support", 1, 0.6, today_str))

    # --- 3. Multi-touch swing points from recent sessions ---
    # Use 5-min bars from the last N days for swing detection
    recent_candles = [c for c in candles
                      if _utc_date(c["time"]) >= (current_date - dt.timedelta(days=lookback_days + 2))
                      and _utc_date(c["time"]) < current_date
                      and SESSION_START_SEC <= _utc_sec(c["time"]) <= SESSION_END_SEC]

    # Resample to 5-min for swing detection
    bars5 = _resample_5min(recent_candles)
    swings = _swing_points(bars5, lookback=10)

    for price, kind in swings:
        source = "swing_high" if kind == "swing_high" else "swing_low"
        direction = "resistance" if kind == "swing_high" else "support"
        zones.append(SRZone(price, source, direction, 1, 0.35, str(current_date - dt.timedelta(days=1))))

    # Cluster nearby zones — audit A 2026-08-23: pass the pre-assigned
    # direction through and use the direction-aware clusterer. The old 3-tuple
    # path re-derived direction from substrings of the type name, so a
    # "prev_close" cluster (no "high"/"low" in the name) silently flipped to
    # "support" regardless of where the close sat in the day's range.
    levels = [(z.price, z.zone_type, z.source_date, z.direction) for z in zones]
    clustered = _cluster_zones_with_direction(levels, tolerance_pct=0.002)

    return clustered


def _resample_5min(candles: list) -> list:
    """Resample 1-min candles to 5-min bars."""
    if not candles:
        return []
    out = []
    cur = None
    for c in sorted(candles, key=lambda x: x["time"]):
        if cur is None:
            cur = {"open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "time": c["time"], "volume": c.get("volume", 0)}
        elif c["time"] < cur["time"] + 300:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c.get("volume", 0)
        else:
            out.append(cur)
            cur = {"open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "time": c["time"], "volume": c.get("volume", 0)}
    if cur:
        out.append(cur)
    return out


def check_proximity(entry: float, stop: float, target: float, bias: str,
                    zones: list[SRZone], buffer_usd: float = 2.0,
                    buffer_pct: float = 0.0) -> tuple[bool, str]:
    """Check if entry/stop/target are too close to S/R zones.

    Per us_stocks audit §5.2:
    - Entry too close to resistance (for long) → skip (resistance may cap the move)
    - Entry too close to support (for short) → skip
    - Stop should be placed BEYOND the opposing zone, not inside it
    - Target should not be right at a zone (partial exit zone)

    Audit B 2026-08-23: buffer is price-scaled when buffer_pct > 0 — an
    absolute $2 buffer on a $0.28 stock (CAN) covered ±700% of price and
    blocked every setup, while being negligible on a $950 one (MU).
    buffer_pct wins when both are provided; buffer_usd stays as legacy.

    Returns (ok, reason). ok=True means the setup passes the S/R check.
    """
    if not zones:
        return True, ""
    buffer_usd = entry * (buffer_pct / 100.0) if buffer_pct > 0 else buffer_usd

    # Sort zones by distance from entry
    nearby = sorted(zones, key=lambda z: z.distance_from(entry))

    for zone in nearby:
        dist = zone.distance_from(entry)
        if dist > buffer_usd * 3:
            break  # zones are sorted, no more close ones

        # --- Entry proximity check ---
        if dist < buffer_usd:
            if bias == "long" and zone.direction == "resistance":
                return False, f"entry ${entry:.2f} too close to resistance ${zone.price:.2f} (${dist:.2f} < ${buffer_usd})"
            if bias == "short" and zone.direction == "support":
                return False, f"entry ${entry:.2f} too close to support ${zone.price:.2f} (${dist:.2f} < ${buffer_usd})"

        # --- Stop placement check ---
        stop_dist = abs(stop - entry)
        if stop_dist > 0:
            if bias == "long" and zone.direction == "support":
                # Stop should be below support zone
                if stop > zone.price and stop_dist < buffer_usd:
                    return False, f"stop ${stop:.2f} inside support zone ${zone.price:.2f}"
            if bias == "short" and zone.direction == "resistance":
                # Stop should be above resistance zone
                if stop < zone.price and stop_dist < buffer_usd:
                    return False, f"stop ${stop:.2f} inside resistance zone ${zone.price:.2f}"

        # --- Target proximity check ---
        target_dist = abs(target - entry)
        if target_dist > 0:
            if bias == "long" and zone.direction == "resistance":
                if abs(target - zone.price) < buffer_usd * 0.5:
                    return False, f"target ${target:.2f} too close to resistance ${zone.price:.2f}"
            if bias == "short" and zone.direction == "support":
                if abs(target - zone.price) < buffer_usd * 0.5:
                    return False, f"target ${target:.2f} too close to support ${zone.price:.2f}"

    return True, ""


def format_zones(zones: list[SRZone]) -> str:
    """Pretty-print zones for logging."""
    if not zones:
        return "  (no zones detected)"
    lines = []
    for z in sorted(zones, key=lambda z: z.price):
        icon = "R" if z.direction == "resistance" else "S"
        lines.append(f"  [{icon}] ${z.price:.2f}  {z.direction:10s}  "
                     f"touches={z.strength}  conf={z.confidence:.0%}  "
                     f"src={z.zone_type}")
    return "\n".join(lines)
