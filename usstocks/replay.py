"""Replay runner — reproduce a full session from CSV bars (ТЗ §11 replay).

Usage:
    python -m usstocks.replay --symbol-csv AMD=data/replay/AMD.csv \
        --benchmark-csv QQQ=data/replay/QQQ.csv --asof "2026-08-26 10:05:00"

- offline by construction (no network imports/calls);
- refuses to run under an auto-trading profile (require_signal_only);
- prints watchlist verdicts, signals with sizing, failed-check reasons.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from typing import Dict, List

from usstocks.data.replay_provider import load_bars
from usstocks.guards import require_signal_only
from usstocks.strategy.vwap_pullback import StrategyConfig, evaluate


def _parse_kv(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"expected SYMBOL=path, got {p!r}")
        k, v = p.split("=", 1)
        out[k.upper()] = v
    return out


def main(argv: List[str] = None) -> int:
    require_signal_only("usstocks.replay")
    ap = argparse.ArgumentParser(prog="usstocks.replay")
    ap.add_argument("--symbol-csv", nargs="*", default=[],
                    help="SYMBOL=path.csv (repeatable)")
    ap.add_argument("--benchmark-csv", action="append", default=[],
                    help="BENCH=path.csv (e.g. QQQ=..., SPY=...)")
    ap.add_argument("--watchlist", default="",
                    help="comma-separated top symbols; empty = all scanned")
    ap.add_argument("--is-tech", default="",
                    help="comma-separated tech names (use QQQ benchmark)")
    ap.add_argument("--asof", default="",
                    help="evaluation moment, NY time; default = last bar close")
    args = ap.parse_args(argv)

    cfg = StrategyConfig()
    symbols = _parse_kv(args.symbol_csv)
    benches = _parse_kv(args.benchmark_csv)
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    tech = {s.strip().upper() for s in args.is_tech.split(",") if s.strip()}

    asof = (datetime.fromisoformat(args.asof) if args.asof else None)
    print(f"replay: {len(symbols)} symbol(s), benchmarks={list(benches)}")

    any_signal = False
    for sym, path in symbols.items():
        bars = load_bars(path, sym)
        bench_key = "QQQ" if sym in tech else "SPY"
        bench_path = benches.get(bench_key)
        if not bench_path and benches:
            bench_path = next(iter(benches.values()))
        bench = load_bars(bench_path, bench_key) if bench_path else []
        t = asof or (bars[-1].ts + timedelta(minutes=5))
        wl_ok = (not watchlist) or sym.upper() in watchlist
        ev_long = evaluate(sym, bars, bench, side="long", in_watchlist=wl_ok,
                           cfg=cfg, asof=t)
        ev_short = evaluate(sym, bars, bench, side="short", in_watchlist=wl_ok,
                            cfg=cfg, asof=t)
        best = max([ev_long, ev_short],
                   key=lambda e: (e.ok, -len(e.failed)))
        status = f"SIGNAL {best.signal.side}" if best.ok else "no signal"
        print(f"\n=== {sym}: {status} ===")
        for line in best.passed:
            print(f"  PASS {line}")
        for line in best.failed:
            print(f"  FAIL {line}")
        if best.ok:
            s = best.signal
            any_signal = True
            print(f"  entry {s.entry_low}-{s.entry_high} stop {s.stop} "
                  f"tp1 {s.tp1} tp2 {s.tp2}")
            print(f"  shares {s.shares}, notional ${s.notional_usd:.0f}, "
                  f"risk ${s.planned_risk_usd:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
