# -*- coding: utf-8 -*-
"""Point-in-time walk-forward бэктест парного модуля (ТЗ §7.2).

Запуск:
  python -m scripts.pairs_backtest [--pair XAU/XAG] [--timeframe D1] [--out path]

Для каждой пары: SignalEngine.walk_forward (β Калмана point-in-time, z по
окну, гейты ADF/HL/Hurst только по данным до входа) -> отчёт в stdout +
JSON/CSV в data/backtest/pairs_backtest_report.json.
"""
import argparse
import datetime as dt
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pairs_analysis import load_config, PairAnalyzer, SignalEngine  # noqa: E402

OUT_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "backtest", "pairs_backtest_report.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair", default=None, help="только одна пара (подстрока имени)")
    ap.add_argument("--timeframe", default=None, help="таймфрейм (по умолчанию из конфига)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    cfg = load_config()
    analysis = cfg.get("analysis", {})
    thresholds = cfg.get("thresholds", {})
    bt_cfg = dict(analysis)
    bt_cfg.update(cfg.get("backtest", {}) or {})
    tf = args.timeframe or analysis.get("default_timeframe", "D1")

    engine = SignalEngine(thresholds, bt_cfg)
    report = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(),
              "timeframe": tf, "pairs": []}

    print(f"=== Pairs backtest ({tf}, point-in-time walk-forward) ===")
    header = (f"{'пара':<16s} | {'n':>4s} {'сделок':>6s} {'WR':>5s} "
              f"{'sumR':>7s} {'avgR':>7s} {'максDD':>7s} {'бар/сделк':>9s} | входы 2-2.5/2.5-3")
    print(header)
    print("-" * len(header))
    for pair in cfg.get("pairs", []):
        if args.pair and args.pair.lower() not in pair["name"].lower():
            continue
        try:
            pa = PairAnalyzer(pair, analysis)
            p1 = pa._load_leg(pair["symbols"][0], tf)
            p2 = pa._load_leg(pair["symbols"][1], tf)
            from pairs_analysis import data as data_mod
            p1, p2 = data_mod.align(p1, p2)
            res = engine.walk_forward(p1, p2, pair["name"], tf)
            s = res.summary()
            entry_str = ""
            if s.get("n_trades"):
                bz = s.get("by_entry_z", {})
                entry_str = (f"{bz.get('2.0-2.5σ', {}).get('n', 0)}/"
                             f"{bz.get('2.5-3.0σ', {}).get('n', 0)}")
            print(f"{pair['name']:<16s} | {s['n_bars']:4d} {s.get('n_trades', 0):6d} "
                  f"{s.get('win_rate', 0):5.1f}% {s.get('sum_r', 0):+7.2f} "
                  f"{s.get('avg_r', 0):+7.3f} {s.get('max_dd_r', 0):7.2f} "
                  f"{s.get('avg_bars_held', 0):9.1f} | {entry_str}")
            report["pairs"].append({"pair": pair["name"], "result": s,
                                    "trades": [t.as_dict() for t in res.trades]})
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"{pair['name']:<16s} | ОШИБКА: {exc}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nОтчёт: {args.out}")


if __name__ == "__main__":
    main()
