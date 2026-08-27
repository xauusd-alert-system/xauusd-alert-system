"""create_initial_data_xauusd - the T-02 "create_initial_data" pattern runner.

Mirrors the NN book's ``create_initial_data.mq5`` (pages 222-229) on the
Python side of the bridge:

    XAUUSD M5 candles -> book feature set (RSI + MACD + candle geometry)
    -> train-only normalization with SAVED parameters
    -> time-ordered 60/20/20 split
    -> artifacts on disk

Artifacts written to ``--out-dir`` (default ``data/book_initial``):

    samples.npz                 X_train/y_train/X_valid/... + target stats
    normalization_params.json   the TRAIN normalization (source of truth)
    book_normalization.json     copy consumed by the EA's FeatureEngine
                                (mql5/NeuroTrader/FeatureEngine.mqh)
    book_day_filter.json        day-of-week filter config for the EA
                                (T-10); disabled unless a trades CSV with
                                own statistics is supplied
    dataset_meta.json           provenance: source, bars, split, columns

If the market-data SQLite has no XAUUSD candles the script falls back to
the deterministic synthetic generator and marks the meta accordingly -
training on synthetic data is allowed only for smoke tests, never for a
deployed model (the WARNING is loud on purpose).

Usage::

    python -m scripts.create_initial_data_xauusd \
        --asset XAUUSD --timeframe M5 --out-dir data/book_initial

    # with own trade statistics for the day filter:
    python -m scripts.create_initial_data_xauusd --trades-csv mytrades.csv
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model.day_of_week_filter import (  # noqa: E402
    blocked_days_from_stats,
    day_of_week_stats,
)
from model.sample_generator import (  # noqa: E402
    DEFAULT_CFG,
    generate_book_samples,
    save_normalization_params,
    synthetic_ohlcv,
)

logger = logging.getLogger("create_initial_data_xauusd")

DEFAULT_DB = os.path.join("data", "market_data_mt5.sqlite")


def load_candles(asset: str, timeframe: str, db_path: str) -> pd.DataFrame | None:
    """OHLCV candles from the project store, time ascending; None if absent."""
    if not os.path.isfile(db_path):
        return None
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in ("candles", "bars"):
            if table not in tables:
                continue
            cols = [r[1] for r in con.execute(
                f"PRAGMA table_info({table})").fetchall()]
            need = {"symbol", "timeframe", "time", "open", "high", "low",
                    "close"}
            if not need.issubset(set(cols)):
                continue
            df = pd.read_sql_query(
                f"SELECT time, open, high, low, close, volume FROM {table} "
                f"WHERE symbol = ? AND timeframe = ? ORDER BY time ASC",
                con, params=(asset, timeframe))
            if len(df) == 0:
                return None
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True
                                        ).dt.tz_localize(None)
            return df.set_index("time")
        return None
    finally:
        con.close()


def load_trades_csv(path: str) -> pd.DataFrame:
    """Own trade log: columns time/open_time, pnl, and optionally win."""
    df = pd.read_csv(path)
    lower = {c.lower(): c for c in df.columns}
    if "pnl" not in lower:
        raise ValueError(f"{path}: need a 'pnl' column for day statistics")
    pnl_col = lower["pnl"]
    time_col = lower.get("time") or lower.get("open_time") or lower.get(
        "close_time")
    if time_col is None:
        raise ValueError(f"{path}: need a time/open_time/close_time column")
    out = pd.DataFrame({
        "time": pd.to_datetime(df[time_col]),
        "pnl": pd.to_numeric(df[pnl_col]),
    })
    if "win" in lower:
        out["win"] = df[lower["win"]].astype(float)
    else:
        out["win"] = (out["pnl"] > 0).astype(float)
    return out


def build_day_filter_config(trades_csv: str | None) -> dict:
    """T-10: the day filter activates only from OWN trade statistics."""
    if not trades_csv:
        return {"enabled": False,
                "min_trades": 30,
                "min_win_rate": 0.45,
                "days_blocked": [],
                "note": "disabled: no --trades-csv supplied (fail-open)"}
    trades = load_trades_csv(trades_csv)
    stats = day_of_week_stats(trades)
    blocked = blocked_days_from_stats(stats, min_trades=30, max_win_rate=0.45)
    return {
        "enabled": bool(blocked),
        "min_trades": 30,
        "min_win_rate": 0.45,
        "days_blocked": sorted(blocked),
        "note": (f"from {trades_csv}: {len(trades)} trades" if blocked
                 else f"from {trades_csv}: no day meets both blocking "
                      f"criteria - filter stays open"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--asset", default="XAUUSD")
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out-dir", default=os.path.join("data",
                                                          "book_initial"))
    parser.add_argument("--synthetic", action="store_true",
                        help="force the synthetic generator (smoke tests)")
    parser.add_argument("--max-bars", type=int, default=None,
                        help="with real candles: use only the most recent N bars")
    parser.add_argument("--bars", type=int, default=20000,
                        help="synthetic bar count (fallback only)")
    parser.add_argument("--window", type=int, default=16)
    parser.add_argument("--horizon", type=int, default=12)
    parser.add_argument("--extended-features", action="store_true",
                        help="T-19 feature set (ATR/session vol/volume)")
    parser.add_argument("--trades-csv", default=None,
                        help="own trade log for the T-10 day filter")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    os.makedirs(args.out_dir, exist_ok=True)

    # ---- 1) source data -------------------------------------------------
    df = None if args.synthetic else load_candles(args.asset, args.timeframe,
                                                  args.db)
    source = f"sqlite:{args.db}"
    if df is None:
        if not args.synthetic:
            logger.warning("no %s %s candles in %s - FALLING BACK TO "
                           "SYNTHETIC DATA (smoke-test grade only, never "
                           "deploy a model trained on it)",
                           args.asset, args.timeframe, args.db)
        df = synthetic_ohlcv(n=args.bars, seed=args.seed)
        source = f"synthetic(seed={args.seed},n={args.bars})"
    elif args.max_bars and len(df) > args.max_bars:
        # long external histories: build artifacts from the most recent regime
        df = df.tail(int(args.max_bars)).reset_index(drop=True)
        logger.info("limited to the most recent %d bars (--max-bars)", len(df))
    logger.info("loaded %d bars from %s", len(df), source)

    # ---- 2) samples (features, train-only normalization, 60/20/20) ------
    cfg = {**DEFAULT_CFG,
           "window": args.window,
           "horizon": args.horizon,
           "extended": bool(args.extended_features)}
    norm_path = os.path.join(args.out_dir, "normalization_params.json")
    samples = generate_book_samples(df, cfg=cfg, norm_params_path=norm_path)

    # ---- 3) artifacts ----------------------------------------------------
    np.savez_compressed(
        os.path.join(args.out_dir, "samples.npz"),
        X_train=samples.X_train, y_train=samples.y_train,
        X_valid=samples.X_valid, y_valid=samples.y_valid,
        X_test=samples.X_test, y_test=samples.y_test,
        target_scale=np.atleast_1d(samples.target_scale),
    )
    # the EA reads its own copy (MQL5\Files\book_normalization.json)
    save_normalization_params(
        samples.norm_params,
        os.path.join(args.out_dir, "book_normalization.json"))

    day_filter = build_day_filter_config(args.trades_csv)
    with open(os.path.join(args.out_dir, "book_day_filter.json"), "w",
              encoding="utf-8") as fh:
        json.dump(day_filter, fh, indent=2)

    meta = {
        "asset": args.asset,
        "timeframe": args.timeframe,
        "source": source,
        "bars": int(len(df)),
        "max_bars": args.max_bars,
        "synthetic": df is not None and source.startswith("synthetic"),
        "feature_columns": list(samples.feature_columns),
        "normalization": samples.norm_params.to_dict(),
        "split": samples.split_sizes(),
        "window": cfg["window"],
        "horizon": cfg["horizon"],
        "extended": cfg["extended"],
        "day_filter": day_filter,
    }
    with open(os.path.join(args.out_dir, "dataset_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    logger.info("samples %s; normalization -> %s",
                samples.split_sizes(), norm_path)
    logger.info("EA artifacts: book_normalization.json, book_day_filter.json "
                "-> copy them into the terminal's MQL5\\Files")
    print(json.dumps({"out_dir": args.out_dir,
                      "split": samples.split_sizes(),
                      "synthetic": meta["synthetic"],
                      "days_blocked": day_filter["days_blocked"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
