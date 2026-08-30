"""Signal-only scanner loop (ТЗ §11 us_stocks_challenge profile).

Pipeline: provider bars -> VWAP Pullback evaluation (both sides) ->
RiskEngine gate -> Notifier. There is NO executor in this object graph by
construction; `python -m usstocks.scanner_loop` additionally refuses to run
under an auto-trading profile (guards.require_signal_only).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Protocol

from config.loader import load_config
from usstocks.guards import require_signal_only
from usstocks.models import Bar, RiskState, TradeSignal
from usstocks.premarket_ranker import TECH_DEFAULTS, ScannerConfig
from usstocks.risk_engine import RiskEngine
from usstocks.session import session_from_cfg
from usstocks.strategy.vwap_pullback import StrategyConfig, evaluate

logger = logging.getLogger("usstocks.loop")


class BarsProvider(Protocol):
    """Minimal provider contract for the loop (UTEX/replay/test fakes)."""

    def get_bars(self, symbol: str, count: int) -> List[Bar]: ...


DEFAULT_SYMBOLS_MAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "backtest", "symbols.json")


def load_symbol_ids(path: str = DEFAULT_SYMBOLS_MAP) -> Dict[str, str]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {str(k).upper(): v for k, v in raw.items()}


class SignalOnlyRunner:
    def __init__(self, cfg: dict, provider: BarsProvider, notifier,
                 *, watchlist: List[str], state: Optional[RiskState] = None,
                 risk: Optional[RiskEngine] = None,
                 symbol_ids: Optional[Dict[str, str]] = None,
                 journal=None,
                 on_event: Optional[Callable] = None):
        self.cfg = cfg
        self.provider = provider
        self.notifier = notifier
        self.watchlist = [s.upper() for s in watchlist]
        self.tech = {t.upper() for t in
                     (cfg.get("us_stocks", {}).get("tech_symbols",
                                                   sorted(TECH_DEFAULTS)))}
        self.strategy_cfg = StrategyConfig.from_cfg(cfg)
        self.risk = risk or RiskEngine.from_cfg(cfg)
        self.session = session_from_cfg(cfg)
        self.state = state or RiskState(session_date="")
        self.symbol_ids = symbol_ids if symbol_ids is not None else load_symbol_ids()
        self.risk_per_trade_usd = float(
            cfg.get("risk", {}).get("risk_per_trade_usd", 10.0))
        self.max_notional_usd = float(
            cfg.get("challenge", {}).get("max_notional_usd", 5000.0))
        self.benchmark_cache: Dict[str, List[Bar]] = {}
        self._risk_notified = set()       # (day, symbol, code) -> TG once/day
        self.journal = journal            # optional usstocks.journal.UsJournal
        self.signals_enabled = True       # toggled by /us_signals on|off
        self.on_event = on_event
        self.warn_latency_threshold_s = float(
            cfg.get("scanner", {}).get("warn_latency_threshold_s", 2.0))
        self.max_workers = int(cfg.get("scanner", {}).get("max_parallel_workers", 3))
        self._cache_ttl_seconds = float(cfg.get("scanner", {}).get("cache_ttl_seconds", 30.0))
        self._vwap_cache: Dict[str, tuple] = {}
        self._or_cache: Dict[str, tuple] = {}
        self._cache_lock = threading.Lock()
        self.metrics: Dict[str, float] = {
            "last_scan_duration_ms": 0.0,
            "total_scans": 0,
            "last_scan_timestamp": 0.0,
        }

    # -- helpers -----------------------------------------------------------

    def _benchmark_for(self, symbol: str) -> str:
        return "QQQ" if symbol in self.tech else "SPY"

    def _bench_bars(self, bench: str) -> List[Bar]:
        if bench not in self.benchmark_cache:
            bid = self.symbol_ids.get(bench)
            self.benchmark_cache[bench] = (
                self.provider.get_bars(bench, 600) if bid else [])
        return self.benchmark_cache[bench]

    def _get_vwap_cached(self, symbol: str, bars: List[Bar], session_date: str) -> List[float]:
        """Get VWAP with caching (invalidate on new bar or TTL)."""
        from usstocks.indicators import session_vwap_series
        cache_key = f"{symbol}_{session_date}"
        bars_hash = hash(tuple((b.ts, b.close, b.volume) for b in bars[-10:]))
        now_ts = time.time()
        with self._cache_lock:
            if cache_key in self._vwap_cache:
                cached_hash, cached_vwap, cached_time = self._vwap_cache[cache_key]
                if cached_hash == bars_hash and (now_ts - cached_time) < self._cache_ttl_seconds:
                    return cached_vwap
        vwap = session_vwap_series(bars)
        with self._cache_lock:
            self._vwap_cache[cache_key] = (bars_hash, vwap, time.time())
        return vwap

    def _get_or_mid_cached(self, symbol: str, bars: List[Bar], session_date: str,
                           opening_range_minutes: int) -> Optional[float]:
        """Get OR mid with caching."""
        from usstocks.indicators import opening_range_mid
        cache_key = f"{symbol}_{session_date}_{opening_range_minutes}"
        bars_hash = hash(tuple((b.ts, b.close) for b in bars[:3]))
        now_ts = time.time()
        with self._cache_lock:
            if cache_key in self._or_cache:
                cached_hash, cached_or, cached_time = self._or_cache[cache_key]
                if cached_hash == bars_hash and (now_ts - cached_time) < self._cache_ttl_seconds:
                    return cached_or
        or_mid = opening_range_mid(bars, opening_range_minutes)
        with self._cache_lock:
            self._or_cache[cache_key] = (bars_hash, or_mid, time.time())
        return or_mid

    def _gate(self, now, symbol: str) -> bool:
        from usstocks.models import RiskEvent
        close_at = self.session.session_close(now.date())
        decision = self.risk.evaluate(self.state, now, close_at, symbol)
        event = RiskEvent(ts=now, code=decision.code,
                          allowed=decision.allowed, reason=decision.reason,
                          symbol=symbol)
        logger.info("risk gate %s %s: %s (%s)", symbol, decision.code,
                    decision.allowed, decision.reason)
        if self.journal:
            try:
                self.journal.save_risk_event(
                    event, session_date=self.state.session_date
                    or now.date().isoformat())
            except Exception:
                logger.exception("journal.save_risk_event failed")
        if self.on_event:
            self.on_event(event)
        if not decision.allowed:
            # ТЗ §8: log every denial, but do not spam Telegram — one message
            # per (day, symbol, code).
            key = (now.date().isoformat(), symbol, decision.code)
            if key not in self._risk_notified:
                self._risk_notified.add(key)
                self.notifier.send_risk_event(event)
        return decision.allowed

    # -- core --------------------------------------------------------------

    def scan_once(self, now) -> List[TradeSignal]:
        """One scan cycle over the watchlist; sends at most one signal.

        Parallelizes per-symbol evaluation (network + strategy) while keeping
        risk gate serial for state consistency. Single-active-position rule
        enforced: first ALLOW wins.
        """
        if not self.signals_enabled:
            logger.info("signals disabled via /us_signals off — skip cycle")
            return []
        start_t = time.perf_counter()
        signals: List[TradeSignal] = []

        # Prefetch benchmarks serially to avoid cache races
        unique_benches = {self._benchmark_for(s) for s in self.watchlist if s in self.symbol_ids}
        for bench in unique_benches:
            try:
                self._bench_bars(bench)
            except Exception as e:
                logger.error("benchmark %s prefetch error %s", bench, e)

        # Cache session_date for VWAP/OR caching
        session_date = self.state.session_date or now.date().isoformat()

        def evaluate_symbol(sym: str):
            sym_start = time.perf_counter()
            if sym not in self.symbol_ids:
                logger.warning("%s: нет symbolId в symbols.json — пропуск", sym)
                return None
            try:
                bars = self.provider.get_bars(sym, 600)
                # Use prefetched benchmark if available
                bench_sym = self._benchmark_for(sym)
                bench = self.benchmark_cache.get(bench_sym, [])
                if not bench:
                    bench = self._bench_bars(bench_sym)
            except Exception as e:
                logger.error("%s: provider error %s", sym, e)
                return None
            try:
                ev_long = evaluate(
                    sym, bars, bench, side="long",
                    in_watchlist=True, cfg=self.strategy_cfg,
                    asof=now,
                    risk_per_trade_usd=self.risk_per_trade_usd,
                    max_notional_usd=self.max_notional_usd,
                    vwap_cache_fn=lambda b, s=sym: self._get_vwap_cached(s, b, session_date),
                    or_cache_fn=lambda b, m, s=sym: self._get_or_mid_cached(s, b, session_date, m),
                )
                ev_short = evaluate(
                    sym, bars, bench, side="short",
                    in_watchlist=True, cfg=self.strategy_cfg,
                    asof=now,
                    risk_per_trade_usd=self.risk_per_trade_usd,
                    max_notional_usd=self.max_notional_usd,
                    vwap_cache_fn=lambda b, s=sym: self._get_vwap_cached(s, b, session_date),
                    or_cache_fn=lambda b, m, s=sym: self._get_or_mid_cached(s, b, session_date, m),
                )
            except Exception as e:
                logger.error("%s: evaluation error %s", sym, e)
                return None
            sym_elapsed = time.perf_counter() - sym_start
            if sym_elapsed > self.warn_latency_threshold_s:
                logger.warning("Latency alert: %s evaluation took %.3fs (> %.1fs threshold)",
                               sym, sym_elapsed, self.warn_latency_threshold_s)
            best = max([ev_long, ev_short], key=lambda e: (e.ok, -len(e.failed)))
            logger.info("%s long failed=%s | short failed=%s",
                        sym, ev_long.failed, ev_short.failed)
            return (sym, best, sym_elapsed)

        max_workers = min(self.max_workers, len(self.watchlist)) if self.watchlist else 1
        # Use parallel only when beneficial
        if max_workers <= 1 or len(self.watchlist) <= 1:
            # Sequential fallback (preserves order)
            results = []
            for sym in self.watchlist:
                r = evaluate_symbol(sym)
                if r is not None:
                    results.append(r)
        else:
            # Parallel execution
            futures = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for sym in self.watchlist:
                    futures[executor.submit(evaluate_symbol, sym)] = sym
                results = []
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        logger.error("%s: evaluation failed: %s", sym, e)
                        continue
                    if result is not None:
                        results.append(result)
            # Preserve watchlist order for deterministic gate priority
            order = {s: i for i, s in enumerate(self.watchlist)}
            results.sort(key=lambda x: order.get(x[0], 999))

        for sym, best, _elapsed in results:
            if not best.ok:
                continue
            signal = best.signal
            if not self._gate(now, sym):
                continue
            signals.append(signal)
            self.state.active_symbol = sym
            self.notifier.send_signal(signal)
            if self.journal:
                try:
                    day = self.state.session_date or now.date().isoformat()
                    self.journal.ensure_session(day)
                    self.journal.save_signal(signal, session_date=day)
                except Exception:
                    logger.exception("journal.save_signal failed")
            break

        total_elapsed_ms = (time.perf_counter() - start_t) * 1000.0
        self.metrics["last_scan_duration_ms"] = round(total_elapsed_ms, 2)
        self.metrics["total_scans"] += 1
        self.metrics["last_scan_timestamp"] = time.time()
        logger.debug("Scan cycle completed in %.2fms", total_elapsed_ms)
        return signals

def run_forever(cfg: dict, runner: SignalOnlyRunner, poll_seconds: float = 60):
    require_signal_only("usstocks.scanner_loop.run_forever")
    while True:
        from datetime import datetime
        try:
            runner.scan_once(datetime.now().astimezone())
        except Exception as e:
            logger.exception("scan cycle failed: %s", e)
        time.sleep(poll_seconds)


def main() -> int:
    require_signal_only("usstocks.scanner_loop")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = load_config(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "us_stocks_challenge.yaml"))
    from usstocks.data.utex_provider import UtexClient
    from usstocks.notify import TelegramNotifier

    client = UtexClient()

    class _Provider:                       # adapter: token refresh per cycle
        @staticmethod
        def get_bars(symbol: str, count: int) -> List[Bar]:
            sid = load_symbol_ids().get(symbol.upper())
            if not sid:
                raise KeyError(f"{symbol}: no symbolId mapping")
            access = client.refresh_access()
            return client.fetch_bars(access, sid, candles_count=count)

    scfg = ScannerConfig.from_cfg(cfg)
    universe = cfg.get("us_stocks", {}).get("base_universe", [])
    runner = SignalOnlyRunner(cfg, _Provider(), TelegramNotifier(),
                              watchlist=universe[:scfg.max_watchlist_size])
    run_forever(cfg, runner,
                poll_seconds=float(cfg.get("scanner", {}).get("poll_seconds", 60)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
