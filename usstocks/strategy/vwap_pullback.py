"""VWAP Pullback Continuation — explainable rule checklist (ТЗ §7.3–§7.4).

Every check produces a stable code; the evaluation returns BOTH lists
(passed / failed) so the journal can always answer "why traded" and "why
not". ML has no vote here (advisory_only per profile config).

Baseline interpretations (documented in docs/STRATEGY_VWAP_PULLBACK.md):
- OR15 filter: confirmation close must be on the momentum side of the
  opening-range midpoint (>= mid for long, <= mid for short);
- "key level" for the 1.8R room test = previous-day high/low plus any
  caller-supplied levels. Levels behind the entry do not block; when no key
  level exists ahead, room is treated as unlimited;
- benchmark VWAP is fail-closed: missing benchmark data blocks the signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from usstocks.indicators import (
    aggregate_to_5m,
    average_volume,
    drop_unclosed_1m,
    opening_range_mid,
    session_vwap_series,
    volume_ratio,
)
from usstocks.models import Bar, TradeSignal
from usstocks.sizing import size_position, targets_from_r


@dataclass
class StrategyConfig:
    opening_range_minutes: int = 15
    min_impulse_pct: float = 0.80          # % rise/fall of the impulse leg
    min_impulse_bars: int = 2              # directed 5m candles in the impulse
    min_impulse_volume_ratio: float = 1.5
    max_pullback_volume_ratio: float = 0.90
    vwap_touch_tolerance_pct: float = 0.10 # % band around VWAP counts as touch
    stop_buffer: float = 0.03              # absolute $ buffer beyond extremes
    min_room_to_level_r: float = 1.8
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    max_pullback_bars: int = 12            # implementation cap / time stop for the scan
    time_stop_bars: int = 12               # maximum allowable bars in pullback before invalidation
    max_spread_pct: float = 0.15           # liquidity check
    commission_per_share: float = 0.0
    fixed_commission: float = 0.0
    min_atr_pct: float = 0.0               # volatility filter: minimum ATR %
    max_atr_pct: Optional[float] = None    # volatility filter: maximum ATR %
    max_climax_volume_ratio: Optional[float] = None  # volume spike filter: max single pullback candle vol ratio
    slippage_cushion_cents: float = 0.0    # adaptive sizing: extra slippage buffer
    use_adaptive_vwap_tolerance: bool = True
    atr_tolerance_multiplier: float = 0.1  # 10% of ATR as tolerance
    max_impulse_counter_moves: int = 1     # allow 1 counter-move candle in impulse

    @classmethod
    def from_cfg(cls, cfg: dict) -> "StrategyConfig":
        s = (cfg or {}).get("strategy", {})
        return cls(
            opening_range_minutes=int(s.get("opening_range_minutes", 15)),
            min_impulse_pct=float(s.get("min_impulse_pct", 0.80)),
            min_impulse_bars=int(s.get("min_impulse_bars", 2)),
            min_impulse_volume_ratio=float(s.get("min_impulse_volume_ratio", 1.5)),
            max_pullback_volume_ratio=float(s.get("max_pullback_volume_ratio", 0.90)),
            vwap_touch_tolerance_pct=float(s.get("vwap_touch_tolerance_pct", 0.10)),
            stop_buffer=float(s.get("stop_buffer", s.get("stop_buffer_cents", 0.03))),
            min_room_to_level_r=float(s.get("min_room_to_level_r", 1.8)),
            tp1_r=float(s.get("tp1_r", 1.0)),
            tp2_r=float(s.get("tp2_r", 2.0)),
            max_pullback_bars=int(s.get("max_pullback_bars", 12)),
            time_stop_bars=int(s.get("time_stop_bars", 12)),
            max_spread_pct=float(s.get("max_spread_pct", 0.15)),
            commission_per_share=float(s.get("commission_per_share", 0.0)),
            fixed_commission=float(s.get("fixed_commission", 0.0)),
            min_atr_pct=float(s.get("min_atr_pct", 0.0)),
            max_atr_pct=float(s["max_atr_pct"]) if "max_atr_pct" in s and s["max_atr_pct"] is not None else None,
            max_climax_volume_ratio=float(s["max_climax_volume_ratio"]) if "max_climax_volume_ratio" in s and s["max_climax_volume_ratio"] is not None else None,
            slippage_cushion_cents=float(s.get("slippage_cushion_cents", 0.0)),
            use_adaptive_vwap_tolerance=bool(s.get("use_adaptive_vwap_tolerance", True)),
            atr_tolerance_multiplier=float(s.get("atr_tolerance_multiplier", 0.1)),
            max_impulse_counter_moves=int(s.get("max_impulse_counter_moves", 1)),
        )


@dataclass
class Evaluation:
    symbol: str
    side: str                              # evaluated direction ("long"/"short")
    signal: Optional[TradeSignal]
    passed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.signal is not None


def _avg_vol_ratio(bars: List[Bar], idxs: List[int], ref_avg: float) -> float:
    if not idxs or ref_avg <= 0:
        return 0.0
    return sum(bars[i].volume for i in idxs) / len(idxs) / ref_avg


def _effective_vwap_tol(cfg: StrategyConfig, bars5: List[Bar]) -> float:
    """Calculate effective VWAP touch tolerance (adaptive or fixed).

    When adaptive is enabled, tolerance = max(config 0.10%, ATR% * multiplier).
    """
    if cfg.use_adaptive_vwap_tolerance and len(bars5) >= 6:
        from usstocks.indicators import calculate_atr
        atr_val = calculate_atr(bars5, period=min(14, len(bars5) - 1))
        atr_pct = (atr_val / bars5[-1].close * 100.0) if bars5[-1].close > 0 else 0.0
        adaptive_tol_pct = max(cfg.vwap_touch_tolerance_pct, atr_pct * cfg.atr_tolerance_multiplier)
        return adaptive_tol_pct / 100.0
    return cfg.vwap_touch_tolerance_pct / 100.0


def _find_structure(bars: List[Bar], vwap: List[float], cfg: StrategyConfig,
                    side: str) -> Optional[dict]:
    """Locate impulse -> pullback -> confirmation ending at the last bar.

    Returns dict with segment indices/levels or None. Deterministic: scans
    impulse-end candidates from earliest to latest and takes the first that
    satisfies impulse + pullback structure.
    """
    n = len(bars)
    if n < cfg.min_impulse_bars + 3:       # impulse + >=1 pullback + confirm
        return None
    up = side == "long"
    sign = 1.0 if up else -1.0
    confirm = n - 1

    for j in range(cfg.min_impulse_bars - 1, confirm - 1):
        # Impulse legs can span more than min_impulse_bars candles: try every
        # plausible window length ending at j, earliest match wins.
        for L in range(cfg.min_impulse_bars, min(7, j + 2)):
            w_start = j - L + 1
            if w_start < 1:            # keep room for a pre-impulse baseline
                continue
            window = bars[w_start:j + 1]

            # --- impulse: directed closes (with tolerance for counter-moves), cumulative move, volume expansion ---
            closes = [b.close for b in window]
            counter_moves = sum(
                1 for k in range(len(closes) - 1)
                if (closes[k + 1] > closes[k]) != up
            )
            directed = counter_moves <= cfg.max_impulse_counter_moves
            if not directed:
                continue
            pre_avg = average_volume(bars, w_start)
            imp_idxs = list(range(w_start, j + 1))
            leg_move = sign * 100.0 * (window[-1].close - window[0].open) / window[0].open
            if leg_move < cfg.min_impulse_pct:
                continue
            imp_vr = _avg_vol_ratio(bars, imp_idxs, pre_avg)
            if imp_vr < cfg.min_impulse_volume_ratio:
                continue

            # --- price above/below VWAP during the impulse ---
            if not all(sign * (bars[i].close - vwap[i]) > 0 for i in imp_idxs):
                continue

            # --- pullback segment ---
            pb = list(range(j + 1, confirm))       # strictly between legs
            max_allowed_pb = min(cfg.max_pullback_bars, cfg.time_stop_bars)
            if not pb or len(pb) > max_allowed_pb:
                continue
            pb_low = min(bars[i].low for i in pb)
            pb_high = max(bars[i].high for i in pb)
            pb_vr = _avg_vol_ratio(bars, pb, pre_avg)
            if pb_vr > cfg.max_pullback_volume_ratio:
                continue
            if cfg.max_climax_volume_ratio is not None:
                single_vrs = [_avg_vol_ratio(bars, [i], pre_avg) for i in pb]
                if any(svr > cfg.max_climax_volume_ratio for svr in single_vrs):
                    continue
            # Retrace must move AGAINST the impulse into the pullback.
            net_retrace = sign * (window[-1].close - bars[confirm - 1].close)
            if net_retrace <= 0:
                continue

            # --- VWAP touch within tolerance during pullback (adaptive) ---
            tol = _effective_vwap_tol(cfg, bars)
            touched = any(
                bars[i].low <= vwap[i] * (1 + tol) if up
                else bars[i].high >= vwap[i] * (1 - tol)
                for i in pb)
            if not touched:
                continue

            swing_window = bars[max(0, w_start - 5):w_start]
            return {
                "impulse": imp_idxs, "pullback": pb, "confirm": confirm,
                "pre_avg": pre_avg, "imp_vr": imp_vr, "pb_vr": pb_vr,
                "leg_move_pct": leg_move,
                "swing_low": min(b.low for b in swing_window),
                "swing_high": max(b.high for b in swing_window),
                "pb_low": pb_low, "pb_high": pb_high,
            }
    return None


def evaluate(symbol: str, bars_1m: List[Bar], bench_1m: List[Bar], *,
             side: str, in_watchlist: bool, cfg: StrategyConfig,
             asof: datetime,
             spread_pct: Optional[float] = None,
             prev_day_levels: Optional[Dict[str, float]] = None,
             extra_levels: Optional[List[float]] = None,
             risk_per_trade_usd: float = 10.0,
             max_notional_usd: float = 5000.0,
             news_blocked: bool = False,
             vwap_cache_fn=None,
             or_cache_fn=None) -> Evaluation:
    """Run the full checklist for one direction; never raises on data gaps."""
    passed: List[str] = []
    failed: List[str] = []

    def check(code: str, ok: bool, detail: str = "") -> bool:
        (passed if ok else failed).append(f"{code}: {detail}" if detail else code)
        return ok

    check("WATCHLIST_MEMBER", in_watchlist,
          "" if in_watchlist else "not in top-3 watchlist")

    if news_blocked:
        check("NEWS_FILTER", False, "high-impact calendar news event nearby")

    if spread_pct is not None and spread_pct > 0:
        check("LIQUIDITY_SPREAD", spread_pct <= cfg.max_spread_pct,
              f"spread {spread_pct:.2f}% > {cfg.max_spread_pct:.2f}%")

    bars5 = aggregate_to_5m(drop_unclosed_1m(bars_1m, asof))
    bench5 = aggregate_to_5m(drop_unclosed_1m(bench_1m, asof))
    if not check("DATA_SUFFICIENT", len(bars5) >= 6,
                 f"{len(bars5)} closed 5m bars"):
        return Evaluation(symbol, side, None, passed, failed)

    # Volatility filter (ATR check if configured)
    if cfg.min_atr_pct > 0 or cfg.max_atr_pct is not None:
        from usstocks.indicators import calculate_atr
        atr_val = calculate_atr(bars5, period=min(14, len(bars5) - 1))
        atr_pct = (atr_val / bars5[-1].close * 100.0) if bars5[-1].close > 0 else 0.0
        vol_ok = atr_pct >= cfg.min_atr_pct and (cfg.max_atr_pct is None or atr_pct <= cfg.max_atr_pct)
        check("VOLATILITY_FILTER", vol_ok, f"ATR {atr_pct:.2f}% (min={cfg.min_atr_pct:.2f}%, max={cfg.max_atr_pct})")

    # VWAP with optional caching callback
    if vwap_cache_fn is not None:
        try:
            vwap = vwap_cache_fn(bars5)
        except Exception:
            vwap = session_vwap_series(bars5)
    else:
        vwap = session_vwap_series(bars5)
    bvwap = session_vwap_series(bench5) if bench5 else []
    ci = len(bars5) - 1
    confirm_bar = bars5[ci]

    # Benchmark VWAP alignment — fail closed.
    bench_ok = bool(bench5) and bool(bvwap) and (
        bench5[-1].close > bvwap[-1] if side == "long"
        else bench5[-1].close < bvwap[-1])
    check("BENCHMARK_VWAP", bench_ok,
          "benchmark on wrong side of its VWAP" if not bench_ok else "")

    st = _find_structure(bars5, vwap, cfg, side)
    if not check("STRUCTURE_IMPULSE_PULLBACK", st is not None,
                 "no impulse->pullback->confirm sequence at last closed bar"):
        return Evaluation(symbol, side, None, passed, failed)

    up = side == "long"
    sign = 1.0 if up else -1.0
    w_start = st["impulse"][0]
    swing_window = bars5[max(0, w_start - 5):w_start]
    pb_extreme = st["pb_low"] if up else st["pb_high"]
    swing_extreme = (min(b.low for b in swing_window) if up
                     else max(b.high for b in swing_window))

    check("IMPULSE_MOVE", st["leg_move_pct"] >= cfg.min_impulse_pct,
          f"{st['leg_move_pct']:+.2f}%")
    check("IMPULSE_VOLUME", st["imp_vr"] >= cfg.min_impulse_volume_ratio,
          f"x{st['imp_vr']:.2f}")
    check("PRICE_TRENDED_VWAP_SIDE",
          all(sign * (bars5[i].close - vwap[i]) > 0 for i in st["impulse"]),
          "impulse stayed on VWAP side")
    check("PULLBACK_VOLUME", st["pb_vr"] <= cfg.max_pullback_volume_ratio,
          f"x{st['pb_vr']:.2f}")

    tol = _effective_vwap_tol(cfg, bars5)
    touched = any(
        bars5[i].low <= vwap[i] * (1 + tol) if up
        else bars5[i].high >= vwap[i] * (1 - tol)
        for i in st["pullback"])
    check("VWAP_TOUCH", touched, "no VWAP touch/probe in pullback")

    confirmed = (confirm_bar.close > vwap[ci]) if up else (confirm_bar.close < vwap[ci])
    check("CONFIRM_CLOSE_VWAP", confirmed, "confirmation close on wrong VWAP side")

    higher_low = (pb_extreme > swing_extreme) if up else (pb_extreme < swing_extreme)
    check("STRUCTURE_HL_LH", higher_low,
          f"{'HL' if up else 'LH'} {pb_extreme:.2f} vs swing {swing_extreme:.2f}")

    if or_cache_fn is not None:
        try:
            or_mid = or_cache_fn(bars5, cfg.opening_range_minutes)
        except Exception:
            or_mid = opening_range_mid(bars5, cfg.opening_range_minutes)
    else:
        or_mid = opening_range_mid(bars5, cfg.opening_range_minutes)
    or_ok = or_mid is not None and (
        confirm_bar.close >= or_mid if up else confirm_bar.close <= or_mid)
    check("OPENING_RANGE_FILTER", or_ok,
          "range incomplete" if or_mid is None else f"or_mid {or_mid:.2f}")

    if failed:
        return Evaluation(symbol, side, None, passed, failed)

    # ---- levels, entry/stop, sizing -------------------------------------
    structure_high = max(bars5[i].high for i in st["pullback"] + [ci]) if up else \
        min(bars5[i].low for i in st["pullback"] + [ci])
    buf = cfg.stop_buffer
    if up:
        entry_trigger = structure_high + buf
        entry_low, entry_high = structure_high, entry_trigger
        stop = pb_extreme - buf
    else:
        entry_trigger = structure_high - buf
        entry_low, entry_high = entry_trigger, structure_high
        stop = pb_extreme + buf

    risk_ps = abs(entry_high - stop)
    levels = dict(prev_day_levels or {})
    ahead = [l for l in list(levels.values()) + list(extra_levels or [])
             if (l > entry_high if up else l < entry_high)]
    nearest = None
    if ahead:
        nearest = min(ahead) if up else max(ahead)
    room_ok = True
    if nearest is not None:
        room_needed = cfg.min_room_to_level_r * risk_ps
        room_ok = abs(nearest - entry_high) >= room_needed
    check("ROOM_TO_LEVEL", room_ok,
          "" if nearest is None else f"nearest level {nearest:.2f}")

    sizing = size_position(entry_high, stop,
                           risk_per_trade_usd=risk_per_trade_usd,
                           max_notional_usd=max_notional_usd,
                           commission_per_share=cfg.commission_per_share,
                           fixed_commission=cfg.fixed_commission)
    check("SIZING", sizing.ok, sizing.reason if not sizing.ok else
          f"{sizing.shares} sh, ${sizing.actual_risk_usd:.2f} risk")
    if failed:
        return Evaluation(symbol, side, None, passed, failed)

    tp1, tp2 = targets_from_r(side, entry_high, stop, cfg.tp1_r, cfg.tp2_r)
    why = [
        f"импульс {st['leg_move_pct']:+.1f}% на объёме x{st['imp_vr']:.1f}",
        "откат к VWAP на слабом объёме (x{:.2f})".format(st["pb_vr"]),
        "подтверждающее закрытие обратно за VWAP",
        "higher low" if up else "lower high",
    ]
    sig = TradeSignal(
        symbol=symbol, side=side,
        entry_low=round(entry_low, 4), entry_high=round(entry_high, 4),
        stop=round(stop, 4), tp1=round(tp1, 4), tp2=round(tp2, 4),
        risk_per_share=round(risk_ps, 4), shares=sizing.shares,
        notional_usd=round(sizing.notional_usd, 2),
        planned_risk_usd=round(sizing.actual_risk_usd, 2),
        grade="B", passed_checks=list(passed), why=why,
        metrics={"impulse_pct": round(st["leg_move_pct"], 3),
                 "impulse_vol_ratio": round(st["imp_vr"], 2),
                 "pullback_vol_ratio": round(st["pb_vr"], 2)},
        created_at=asof,
    )
    return Evaluation(symbol, side, sig, passed, failed)


def evaluate_both_sides(symbol, bars_1m, bench_long, bench_short, **kw) -> Evaluation:
    """Long uses QQQ-style benchmark; short may use SPY (ТЗ §7.4 mirror)."""
    res_long = evaluate(symbol, bars_1m, bench_long, side="long", **kw)
    res_short = evaluate(symbol, bars_1m, bench_short, side="short", **kw)
    if res_long.ok:
        return res_long
    if res_short.ok:
        return res_short
    return res_long if len(res_long.failed) <= len(res_short.failed) else res_short
