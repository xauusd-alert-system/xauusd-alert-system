"""Задача 1.1 (ТЗ strategy-improvements): распределение vol_pct = atr / close.

Research-only script. Не меняет продовый код, не пишет в БД, не коммитит CSV.

Для XAUUSD, EURUSD, GBPUSD на ohlcv_m15 (data/market_data_mt5.sqlite,
до 2026-08-08 включительно) считаем:

* ATR(period=14) ровно как в features/indicators.atr (EWM true-range,
  alpha=1/14, min_periods=14) — тот же ATR, который попадает в
  adaptive_holding_period() в labeling/label_generator.py при
  adaptive_holding=true;
* vol_pct = atr / close (та же нормировка, что в labeling).

Вывод: mean, p50, p75, p90, p95, p99, max по каждому активу + доля баров,
которые превысили бы текущие продовые пороги (0.02 / 0.01) и
кандидатные перцентильные пороги (p95/p75).
"""

import sys as _sys

# Windows-консоли часто доступен только cp1252 — форсируем UTF-8 для вывода.
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# Позволяем запускать файл напрямую: python scripts/research/vol_pct_distribution.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from data.storage import read_candles  # noqa: E402
from features.indicators import atr  # noqa: E402

ASSETS = ["XAUUSD", "EURUSD", "GBPUSD"]
DEFAULT_DB = "data/market_data_mt5.sqlite"
DEFAULT_END_DATE = "2026-08-08"
ATR_PERIOD = 14  # config/config.yaml: features.atr_period

STAT_COLUMNS = ["mean", "p50", "p75", "p90", "p95", "p99", "max"]

# Текущие продовые пороги (config/config.yaml, labeling.*)
PROD_HIGH_VOL_PCT = 0.02
PROD_MID_VOL_PCT = 0.01


def load_vol_pct(db_path: str, symbol: str, end_ts: int) -> pd.Series:
    """vol_pct = atr / close для одного актива на M15, NaN отброшены."""
    df = read_candles(db_path, "m15", symbol, end_ts=end_ts)
    if df.empty:
        raise RuntimeError(f"No M15 candles for {symbol} in {db_path}")
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    df["atr"] = atr(df, ATR_PERIOD)
    vol_pct = df["atr"] / df["close"]
    return vol_pct.dropna().reset_index(drop=True)


def describe(series: pd.Series) -> dict:
    return {
        "n_bars": int(len(series)),
        "mean": float(series.mean()),
        "p50": float(series.quantile(0.50)),
        "p75": float(series.quantile(0.75)),
        "p90": float(series.quantile(0.90)),
        "p95": float(series.quantile(0.95)),
        "p99": float(series.quantile(0.99)),
        "max": float(series.max()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--json-out", default=None, help="опциональный путь для raw JSON")
    args = parser.parse_args()

    end_ts = int(pd.Timestamp(args.end_date, tz="UTC").timestamp())

    rows: dict[str, dict] = {}
    for symbol in ASSETS:
        vol_pct = load_vol_pct(args.db, symbol, end_ts)
        stats = describe(vol_pct)
        rows[symbol] = stats

        # Диагностика против продовых порогов
        frac_above_mid = float((vol_pct > PROD_MID_VOL_PCT).mean())
        frac_above_high = float((vol_pct > PROD_HIGH_VOL_PCT).mean())
        stats["frac_gt_prod_mid_0.01"] = frac_above_mid
        stats["frac_gt_prod_high_0.02"] = frac_above_high

        first_ts = read_candles(args.db, "m15", symbol, end_ts=end_ts)["timestamp_utc"]
        stats["first_bar_utc"] = str(pd.Timestamp(int(first_ts.min()), unit="s", tz="UTC"))
        stats["last_bar_utc"] = str(pd.Timestamp(int(first_ts.max()), unit="s", tz="UTC"))

    # Таблица в stdout
    header = f"{'asset':8s} " + " ".join(f"{c:>12s}" for c in STAT_COLUMNS) + f" {'n_bars':>9s}"
    print(header)
    print("-" * len(header))
    for symbol, stats in rows.items():
        cells = " ".join(f"{stats[c]:12.6f}" for c in STAT_COLUMNS)
        print(f"{symbol:8s} {cells} {stats['n_bars']:9d}")

    print()
    print("Доля баров выше продовых порогов / кандидатные перцентильные пороги:")
    for symbol, stats in rows.items():
        print(
            f"{symbol:8s} >0.01: {stats['frac_gt_prod_mid_0.01']*100:6.2f}%   "
            f">0.02: {stats['frac_gt_prod_high_0.02']*100:6.2f}%   "
            f"p75={stats['p75']:.6f}  p95={stats['p95']:.6f}  p99={stats['p99']:.6f}"
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nJSON saved to {args.json_out}")


if __name__ == "__main__":
    main()
