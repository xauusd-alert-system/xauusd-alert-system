# -*- coding: utf-8 -*-
"""Integration layer for the pairs module (ТЗ §4.6-§4.8).

Bridges PairAnalyzer/SignalEngine/EnsembleEngine with the existing system:
  - Scanner watchlist: VALID_MEANREV / NO_EDGE / INVALID per pair
  - Risk calculator: hedge ratio, stop in $, position sizing
  - Journal: new fields for pair trades + weekly metrics
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import metrics as metrics_mod
from .analyzer import PairAnalyzer, PairMetrics, BARS_PER_DAY, BARS_PER_YEAR
from .signal import SignalEngine, Signal
from .ensemble import EnsembleEngine, EnsembleForecast


# ============================================================================
# §4.6 Scanner watchlist integration
# ============================================================================

@dataclass
class PairWatchlistEntry:
    """One pair's status for the scanner watchlist (ТЗ §4.6)."""
    name: str
    status: str                          # VALID_MEANREV | NO_EDGE | INVALID
    direction: str                       # long | short | none
    z: float = 0.0
    score: float = 0.0                   # prioritization score (higher = better)
    adf_p: float = 1.0
    half_life_days: float = 0.0
    hurst: float = 0.5
    ensemble_confidence: float = 0.0
    ensemble_direction: str = "neutral"
    beta: float = 0.0
    sigma: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def pair_score(m: PairMetrics, sig: Signal, ens: EnsembleForecast,
               weights: dict | None = None) -> float:
    """Prioritization score for pair watchlist (ТЗ §4.6).

    score = w1·|z|/entry_z + w2·(1 - adf_p) + w3·(ensemble_conf/100)
            + w4·(1 - |H-0.5|·2) for mean-rev signals.
    Higher = more actionable.
    """
    w = weights or {}
    w1 = w.get("z", 0.4)
    w2 = w.get("adf", 0.2)
    w3 = w.get("ensemble", 0.25)
    w4 = w.get("hurst", 0.15)

    z_norm = min(abs(sig.z) / 2.0, 2.0) if sig.z != 0 else 0  # normalized to entry_z
    adf_score = max(0, 1.0 - m.adf_p / 0.10) if np.isfinite(m.adf_p) else 0
    ens_score = ens.confidence / 100.0 if ens else 0
    hurst_score = max(0, 1.0 - abs(m.hurst - 0.5) * 2) if np.isfinite(m.hurst) else 0

    return round(w1 * z_norm + w2 * adf_score + w3 * ens_score + w4 * hurst_score, 4)


def scan_pairs(cfg: dict, thresholds: dict | None = None,
               timeframe: str = "D1") -> list[PairWatchlistEntry]:
    """Analyze all configured pairs and return watchlist entries (ТЗ §4.6).

    Status logic:
      - INVALID: ADF p >= adf_p_max OR HL outside range
      - VALID_MEANREV: all gates pass AND |z| >= entry_z (signal active)
      - NO_EDGE: gates pass but |z| < entry_z (waiting for entry)
    """
    analysis = cfg.get("analysis", {})
    bt_cfg = dict(analysis)
    bt_cfg.update(cfg.get("backtest", {}) or {})
    th = thresholds or cfg.get("thresholds", {})
    sig_engine = SignalEngine(th, bt_cfg)
    ens_engine = EnsembleEngine(cfg)

    entries = []
    for pair in cfg.get("pairs", []):
        name = pair["name"]
        try:
            pa = PairAnalyzer(pair, analysis)
            m = pa.analyze(timeframe)
            sig = sig_engine.current(m)
            ens = ens_engine.forecast(m)

            # Status determination
            adf_p_max = float(analysis.get("adf_p_max", 0.05))
            hl_range = analysis.get("half_life_range_days", [1, 60])
            hl_days = m.half_life_days

            hl_ok = np.isfinite(hl_days) and hl_range[0] <= hl_days <= hl_range[1]
            adf_ok = np.isfinite(m.adf_p) and m.adf_p < adf_p_max
            hurst_ok = np.isfinite(m.hurst) and m.hurst < float(analysis.get("hurst_meanrev_max", 0.5))

            if not (adf_ok and hl_ok):
                status = "INVALID"
                reason = []
                if not adf_ok:
                    reason.append(f"ADF p={m.adf_p:.3f}")
                if not hl_ok:
                    reason.append(f"HL={hl_days:.1f}d")
                direction = "none"
            elif sig.valid:
                status = "VALID_MEANREV"
                direction = sig.direction
                reason = [sig.reason]
            else:
                status = "NO_EDGE"
                direction = "none"
                reason = [sig.reason]

            sc = pair_score(m, sig, ens) if status != "INVALID" else 0

            entries.append(PairWatchlistEntry(
                name=name, status=status, direction=direction,
                z=sig.z, score=sc,
                adf_p=m.adf_p, half_life_days=hl_days, hurst=m.hurst,
                ensemble_confidence=ens.confidence,
                ensemble_direction=ens.direction,
                beta=m.beta, sigma=m.sigma,
                reason="; ".join(reason),
            ))
        except Exception as e:
            entries.append(PairWatchlistEntry(
                name=name, status="INVALID", direction="none",
                reason=str(e)))

    # Sort by score descending (best opportunities first)
    entries.sort(key=lambda e: e.score, reverse=True)
    return entries


# ============================================================================
# §4.7 Risk calculator integration
# ============================================================================

@dataclass
class PairPosition:
    """Computed position for a pair trade (ТЗ §4.7)."""
    pair_name: str
    direction: str                       # long | short (direction of the spread)
    hedge_ratio: float                   # β (Kalman)
    hedge_mode: str                      # dollar_neutral | log_neutral
    # Leg sizing
    p1_symbol: str
    p1_contracts: float
    p1_price: float
    p2_symbol: str
    p2_contracts: float
    p2_price: float
    # Risk
    risk_usd: float
    stop_spread_z: float                 # stop in σ units
    stop_spread_usd: float               # stop in dollar terms
    # Expected trade
    half_life_days: float
    vol_adjustment: float = 1.0          # k multiplier (σ_target / σ_current)
    # Info
    spread_sigma: float = 0.0
    spread_current: float = 0.0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def size_pair_position(m: PairMetrics, sig: Signal, risk_usd: float,
                       target_spread_vol: float | None = None,
                       vol_k_range: tuple[float, float] = (0.5, 1.5),
                       hedge_mode: str = "dollar_neutral") -> PairPosition:
    """Compute position size for a pair trade (ТЗ §4.7).

    risk_usd: maximum dollar risk for the trade.
    target_spread_vol: target annualized spread volatility for vol adjustment.
    hedge_mode: 'dollar_neutral' (n1·P1 = |β|·n2·P2) or 'log_neutral' (n1 = β·n2).
    """
    beta = m.beta
    sigma = m.sigma
    z_cur = sig.z
    stop_z = 3.0  # from thresholds

    # Stop distance in spread σ units
    stop_spread_z = stop_z - abs(z_cur)

    # Vol adjustment: k = σ_target / σ_current, clamped
    if target_spread_vol and sigma > 0:
        k = target_spread_vol / sigma
        k = max(vol_k_range[0], min(vol_k_range[1], k))
    else:
        k = 1.0

    # Risk in dollar terms (risk_usd already accounts for vol adjustment)
    risk_adjusted = risk_usd * k

    # Stop in dollar terms for the spread
    spread_std_usd = sigma * m.p1_last  # approximate: σ of spread in price terms
    stop_spread_usd = stop_spread_z * spread_std_usd if spread_std_usd > 0 else risk_adjusted

    p1_sym = m.name.split("/")[0] + "USD" if "/" in m.name else m.name
    p2_sym = m.name.split("/")[1] + "USD" if "/" in m.name else m.name

    if hedge_mode == "dollar_neutral":
        # n1·P1 = |β|·n2·P2  AND  risk_usd = n1·P1·stop_z (approx)
        # Simplified: n1 = risk_usd / (P1 · stop_spread_z · σ)
        p1_price = m.p1_last
        p2_price = m.p2_last
        if p1_price > 0 and stop_spread_z > 0:
            p1_contracts = risk_adjusted / (p1_price * stop_spread_z * sigma) if sigma > 0 else 0
        else:
            p1_contracts = 0
        p2_contracts = abs(beta) * p1_contracts * p1_price / p2_price if p2_price > 0 else 0
    else:
        # log_neutral: n1 = β · n2
        p1_price = m.p1_last
        p2_price = m.p2_last
        if p1_price > 0:
            p1_contracts = risk_adjusted / (p1_price * stop_spread_z * sigma) if sigma > 0 else 0
        else:
            p1_contracts = 0
        p2_contracts = p1_contracts / abs(beta) if abs(beta) > 0 else 0

    return PairPosition(
        pair_name=m.name, direction=sig.direction,
        hedge_ratio=beta, hedge_mode=hedge_mode,
        p1_symbol=p1_sym, p1_contracts=round(p1_contracts, 4),
        p1_price=m.p1_last,
        p2_symbol=p2_sym, p2_contracts=round(p2_contracts, 4),
        p2_price=m.p2_last,
        risk_usd=round(risk_adjusted, 2),
        stop_spread_z=round(stop_spread_z, 3),
        stop_spread_usd=round(stop_spread_usd, 2),
        half_life_days=m.half_life_days,
        vol_adjustment=round(k, 3),
        spread_sigma=sigma,
        spread_current=float(m.spread.dropna().iloc[-1]) if len(m.spread.dropna()) else 0,
    )


# ============================================================================
# §4.8 Journal integration
# ============================================================================

PAIR_JOURNAL_FIELDS = [
    "num", "date", "time", "pair", "direction", "spread_direction",
    "entry_z", "exit_z", "exit_reason", "r", "bars_held",
    "beta", "hedge_mode", "risk_usd", "p1_symbol", "p1_contracts",
    "p2_symbol", "p2_contracts", "adf_p", "half_life_days", "hurst",
    "regime", "ensemble_direction", "ensemble_confidence",
    "z_on_exit", "resolved_utc",
]

PAIR_JOURNAL_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "manual", "pair_journal.csv")


def _ensure_pair_journal(path: str) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(PAIR_JOURNAL_FIELDS)


def log_pair_trade(path: str, date: str, time_str: str,
                   pair: str, direction: str, spread_direction: str,
                   entry_z: float, exit_z: float, exit_reason: str,
                   r: float, bars_held: int,
                   beta: float, hedge_mode: str, risk_usd: float,
                   p1_symbol: str, p1_contracts: float,
                   p2_symbol: str, p2_contracts: float,
                   adf_p: float, half_life_days: float, hurst: float,
                   regime: str, ensemble_direction: str,
                   ensemble_confidence: float,
                   z_on_exit: float = 0.0,
                   num: int | None = None) -> int:
    """Append one pair trade to the journal (ТЗ §4.8)."""
    _ensure_pair_journal(path)
    if num is None:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        num = int(rows[-1]["num"]) + 1 if rows else 1
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PAIR_JOURNAL_FIELDS)
        w.writerow({
            "num": num, "date": date, "time": time_str,
            "pair": pair, "direction": direction,
            "spread_direction": spread_direction,
            "entry_z": entry_z, "exit_z": exit_z,
            "exit_reason": exit_reason, "r": r, "bars_held": bars_held,
            "beta": beta, "hedge_mode": hedge_mode,
            "risk_usd": risk_usd,
            "p1_symbol": p1_symbol, "p1_contracts": p1_contracts,
            "p2_symbol": p2_symbol, "p2_contracts": p2_contracts,
            "adf_p": adf_p, "half_life_days": half_life_days,
            "hurst": hurst, "regime": regime,
            "ensemble_direction": ensemble_direction,
            "ensemble_confidence": ensemble_confidence,
            "z_on_exit": z_on_exit,
            "resolved_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
    return num


def read_pair_journal(path: str = PAIR_JOURNAL_DEFAULT) -> list[dict]:
    _ensure_pair_journal(path)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pair_weekly_metrics(path: str = PAIR_JOURNAL_DEFAULT) -> list[dict]:
    """Weekly aggregates for pair trades (ТЗ §4.8): win-rate, avg R, by z-bucket,
    by regime, avg hold time vs half-life."""
    rows = read_pair_journal(path)
    weeks = {}
    for r in rows:
        try:
            d = dt.date.fromisoformat(r["date"])
        except ValueError:
            continue
        key = d.isocalendar()[:2]
        weeks.setdefault(key, []).append(r)

    out = []
    for key, rs in weeks.items():
        rr = []
        by_z_buckets = {"2.0-2.5": [], "2.5-3.0": []}
        by_regime = {"trending": [], "mean-reverting": [], "mixed": []}
        hold_vs_hl = []

        for r in rs:
            try:
                val = float(r["r"])
                rr.append(val)
            except (ValueError, KeyError):
                continue

            # z-bucket
            try:
                ez = abs(float(r["entry_z"]))
                if 2.0 <= ez < 2.5:
                    by_z_buckets["2.0-2.5"].append(val)
                elif 2.5 <= ez < 3.0:
                    by_z_buckets["2.5-3.0"].append(val)
            except (ValueError, KeyError):
                pass

            # regime
            regime = r.get("regime", "").lower()
            if regime in by_regime:
                by_regime[regime].append(val)

            # hold time vs half-life
            try:
                held = float(r["bars_held"])
                hl = float(r["half_life_days"])
                if hl > 0:
                    hold_vs_hl.append(held / hl)
            except (ValueError, KeyError):
                pass

        n = len(rr)
        wins = sum(1 for v in rr if v > 0)
        losses = sum(1 for v in rr if v < 0)

        def _avg(lst):
            return round(sum(lst) / len(lst), 3) if lst else None

        out.append({
            "iso_week": "%d-W%02d" % key,
            "trades": n,
            "wins": wins, "losses": losses,
            "avg_r": _avg(rr),
            "win_rate_pct": round(100 * wins / n, 1) if n else 0,
            "by_entry_z": {k: {"n": len(v), "avg_r": _avg(v)}
                           for k, v in by_z_buckets.items()},
            "by_regime": {k: {"n": len(v), "avg_r": _avg(v)}
                          for k, v in by_regime.items()},
            "hold_vs_hl_ratio": _avg(hold_vs_hl),
        })
    return out


def pair_cumulative_stats(path: str = PAIR_JOURNAL_DEFAULT) -> dict:
    """Cumulative stats across all pair trades."""
    rows = read_pair_journal(path)
    rr = []
    for r in rows:
        try:
            rr.append(float(r["r"]))
        except (ValueError, KeyError):
            continue
    n = len(rr)
    wins = sum(1 for v in rr if v > 0)
    losses = sum(1 for v in rr if v < 0)
    return {
        "total_trades": n,
        "wins": wins, "losses": losses, "flat": n - wins - losses,
        "sum_r": round(sum(rr), 3),
        "avg_r": round(sum(rr) / n, 3) if n else None,
        "win_rate_pct": round(100 * wins / n, 1) if n else None,
        "max_r": round(max(rr), 3) if rr else None,
        "min_r": round(min(rr), 3) if rr else None,
    }
