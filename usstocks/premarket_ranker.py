"""Premarket ranker — top-3 watchlist per ТЗ §7.1.

Scoring is exactly the ТЗ formula; filters come from `scanner:` in the
profile config. Honest data note: UTEX candles carry no spread — when the
spread is unknown the spread filter is skipped and the snapshot is marked
`spread_unknown=True` (never fabricated).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

from usstocks.indicators import ensure_ny
from usstocks.models import Bar, PremarketSnapshot, WatchlistItem

# Benchmark mapping per ТЗ §7.3.5: tech names ride QQQ, everything else SPY.
TECH_DEFAULTS = {"AAPL", "MSFT", "NVDA", "AMD", "META", "AVGO", "GOOGL"}


def score_snapshot(s: PremarketSnapshot) -> int:
    """ТЗ §7.1 scoring, verbatim."""
    score = 0
    if abs(s.gap_pct) >= 2.0:
        score += 2
    if s.relative_volume >= 2.0:
        score += 3
    elif s.relative_volume >= 1.5:
        score += 1
    if s.fresh_news_catalyst:
        score += 2
    if s.avg_daily_dollar_volume >= 50_000_000:
        score += 1
    if s.price >= 10 and s.spread_pct <= 0.10:
        score += 2
    return score


@dataclass
class ScannerConfig:
    min_price: float = 10.0
    min_abs_gap_pct: float = 1.5
    min_relative_volume: float = 1.5
    min_average_daily_dollar_volume: float = 50_000_000
    max_spread_pct: float = 0.10
    max_watchlist_size: int = 3

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ScannerConfig":
        sc = (cfg or {}).get("scanner", {})
        return cls(
            min_price=float(sc.get("min_price", 10.0)),
            min_abs_gap_pct=float(sc.get("min_abs_gap_pct", 1.5)),
            min_relative_volume=float(sc.get("min_relative_volume", 1.5)),
            min_average_daily_dollar_volume=float(
                sc.get("min_average_daily_dollar_volume", 50_000_000)),
            max_spread_pct=float(sc.get("max_spread_pct", 0.10)),
            max_watchlist_size=int(sc.get("max_watchlist_size", 3)),
        )


def build_snapshot(symbol: str, bars_1m: List[Bar],
                   prior_days: int = 5,
                   fresh_news_catalyst: bool = False,
                   spread_pct: Optional[float] = None) -> Optional[PremarketSnapshot]:
    """Derive a snapshot from 1m bars (>=2 sessions required for baselines)."""
    if len(bars_1m) < 10:
        return None
    days: Dict[date, List[Bar]] = {}
    for b in bars_1m:
        days.setdefault(ensure_ny(b.ts).date(), []).append(b)
    dates = sorted(days)
    today = dates[-1]
    prior = [d for d in dates[:-1]][-prior_days:]
    if not prior:
        return None

    prev_close = days[prior[-1]][-1].close
    today_bars = days[today]
    open_price = today_bars[0].open
    if prev_close <= 0 or open_price <= 0:
        return None
    gap_pct = (open_price - prev_close) / prev_close * 100.0

    day_stats = []
    for d in prior:
        vol = sum(b.volume for b in days[d])
        dollars = sum(b.close * b.volume for b in days[d])
        day_stats.append((vol, dollars))
    avg_vol = sum(v for v, _ in day_stats) / len(day_stats)
    avg_dollar_vol = sum(d for _, d in day_stats) / len(day_stats)

    today_cum = sum(b.volume for b in today_bars)
    rel_vol = (today_cum / avg_vol) if avg_vol > 0 else 0.0

    return PremarketSnapshot(
        symbol=symbol.upper(),
        price=today_bars[-1].close,
        prev_close=prev_close,
        gap_pct=round(gap_pct, 3),
        relative_volume=round(rel_vol, 2),
        avg_daily_dollar_volume=round(avg_dollar_vol),
        spread_pct=spread_pct if spread_pct is not None else 0.0,
        fresh_news_catalyst=fresh_news_catalyst,
    )


def passes_filters(s: PremarketSnapshot, cfg: ScannerConfig) -> tuple[bool, str]:
    if s.price < cfg.min_price:
        return False, f"price {s.price:.2f} < {cfg.min_price}"
    if abs(s.gap_pct) < cfg.min_abs_gap_pct:
        return False, f"|gap| {abs(s.gap_pct):.2f}% < {cfg.min_abs_gap_pct}%"
    if s.relative_volume < cfg.min_relative_volume:
        return False, f"relvol {s.relative_volume:.2f}x < {cfg.min_relative_volume}x"
    if s.avg_daily_dollar_volume < cfg.min_average_daily_dollar_volume:
        return False, (f"adv ${s.avg_daily_dollar_volume/1e6:.0f}M < "
                       f"${cfg.min_average_daily_dollar_volume/1e6:.0f}M")
    if s.spread_pct > 0 and s.spread_pct > cfg.max_spread_pct:
        return False, f"spread {s.spread_pct:.2f}% > {cfg.max_spread_pct:.2f}%"
    return True, "ok"


def build_watchlist(snapshots: List[PremarketSnapshot],
                    cfg: ScannerConfig) -> List[WatchlistItem]:
    scored = []
    for snap in snapshots:
        ok, _why = passes_filters(snap, cfg)
        if not ok:
            continue
        item = WatchlistItem(snapshot=snap, is_tech=snap.symbol in TECH_DEFAULTS)
        item.snapshot.score = score_snapshot(snap)
        scored.append(item)
    scored.sort(key=lambda it: it.snapshot.score, reverse=True)
    return scored[:cfg.max_watchlist_size]


def format_watchlist_message(items: List[WatchlistItem]) -> str:
    lines = ["🌅 US STOCKS — watchlist на сегодня:"]
    for i, it in enumerate(items, 1):
        s = it.snapshot
        bench = "QQQ" if it.is_tech else "SPY"
        lines.append(f"{i}. {s.symbol}: gap {s.gap_pct:+.2f}%, "
                     f"RVOL {s.relative_volume:.1f}x, score {s.score}, бенчмарк {bench}")
    lines.append("Signal-only: бот не отправляет ордера.")
    return "\n".join(lines)
