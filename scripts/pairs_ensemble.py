# -*- coding: utf-8 -*-
"""Ensemble forecast for all configured pairs (ТЗ §4.3).

Usage:
    PYTHONIOENCODING=utf-8 python -m scripts.pairs_ensemble [--pair XAU/XAG] [--timeframe D1]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pairs_analysis import load_config, PairAnalyzer, EnsembleEngine


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair", default=None)
    ap.add_argument("--timeframe", default=None)
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args()

    cfg = load_config()
    analysis = cfg.get("analysis", {})
    tf = args.timeframe or analysis.get("default_timeframe", "D1")
    ens = EnsembleEngine(cfg)

    results = []
    for pair in cfg.get("pairs", []):
        if args.pair and args.pair.lower() not in pair["name"].lower():
            continue
        try:
            pa = PairAnalyzer(pair, analysis)
            m = pa.analyze(tf)
            f = ens.forecast(m)
            results.append(f)
        except Exception as e:
            print(f"{pair['name']}: ERROR {e}", file=sys.stderr)

    if args.json:
        print(json.dumps([f.as_dict() for f in results], ensure_ascii=False, indent=2))
    else:
        for f in results:
            print(f"{f.summary_line()}")
            for e in f.engines:
                d = e.details
                extra = ", ".join(f"{k}={v}" for k, v in list(d.items())[:3])
                print(f"  {e.name:15s}: {e.direction:8s} {e.confidence:5.1f}% | {extra}")
            print()


if __name__ == "__main__":
    main()
