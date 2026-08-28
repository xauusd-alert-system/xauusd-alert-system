"""
Diagnostic for GBPUSD profile under current FX v4 config (trend-friendly, stop 3.0, BE at TP1, H1).
- Loads H1 GBPUSD via scripts.train_mt5.build_full_df (with order-flow).
- Runs EnsembleBacktester with current GBPUSD asset config.
- Outputs to logs/diag_gbp_profile.csv:
  entry bar, exit bar, entry/exit price, exit_reason, pnl,
  max favorable excursion (mfe), max adverse excursion (mae),
  and how many BE scratches would have reached TP2/TP3 later.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.ingestion import to_epoch_seconds  # for smoke tests / no DB
from model.ensemble_backtest import EnsembleBacktester
from scripts.train_mt5 import build_full_df


def _make_synthetic_gbp_df(n=2000, price=1.30, atr=0.0015, seed=42):
    """Synthetic GBP H1 with ATR, session, regime, ml_p_long/short."""
    np.random.seed(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC")
    t = np.arange(n)
    closes = price + 0.0001 * np.sin(t / 50.0) + np.cumsum(np.random.randn(n) * atr * 0.2)
    opens = closes + np.random.randn(n) * atr * 0.05
    highs = np.maximum(opens, closes) + np.abs(np.random.randn(n)) * 0.0008 + 0.0002
    lows = np.minimum(opens, closes) - np.abs(np.random.randn(n)) * 0.0008 - 0.0002
    df = pd.DataFrame({
        "timestamp_utc": to_epoch_seconds(idx),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": 1000.0,
        "session": np.random.choice(["london", "newyork", "asia"], n, p=[0.45, 0.35, 0.20]),
        "regime": np.random.choice(["trend_up", "trend_down", "range", "compression"], n,
                                   p=[0.3, 0.3, 0.3, 0.1]),
        "atr": atr,
    })
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic mock data")
    args = parser.parse_args()

    cfg = load_config()
    asset_key = "GBPUSD"
    asset_cfg = cfg["assets"][asset_key]
    timeframe = asset_cfg.get("timeframe") or "H1"
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    if not args.synthetic:
        try:
            raw = pd.read_sql_query(
                "SELECT * FROM candles WHERE asset=? AND timeframe=? ORDER BY timestamp_utc",
                __import__("sqlite3").connect(db_path), params=(asset_key, timeframe))
            df = build_full_df(
                raw, cfg, db_path=db_path, asset_key=asset_key, timeframe=timeframe
            )
            # Score the frame with the PRODUCTION model so the diagnostics profile
            # the same entries the live trader would take. Without this the
            # backtester's ml_p defaults to 0.5 and every signal is declined
            # (min_ml_probability 0.55) -> the real-data diagnostics find nothing.
            model_path = asset_cfg.get("model_path")
            if model_path and os.path.exists(model_path):
                from model.predictor import ModelPredictor
                predictor = ModelPredictor(model_path)
                preds = predictor.predict_proba(df.fillna(0.0))
                df["ml_p_long"] = preds["p_long"].values
                df["ml_p_short"] = preds["p_short"].values
            else:
                print(f"[diag] WARNING: production model not found at {model_path}; "
                      "no ML entries will be generated on the real-data slice.")
        except Exception as e:
            print(f"[diag] No usable DB or error ({e}); using synthetic mock for GBPUSD profile.")
            raw = _make_synthetic_gbp_df()
            df = raw.copy()
            df["ml_p_long"] = np.clip(0.5 + (df["close"].diff().fillna(0) > 0).astype(float) * 0.35 + np.random.randn(len(df)) * 0.1, 0.1, 0.9)
            df["ml_p_short"] = 1.0 - df["ml_p_long"]
    else:
        df = _make_synthetic_gbp_df()
        df["ml_p_long"] = np.clip(0.5 + (df["close"].diff().fillna(0) > 0).astype(float) * 0.35 + np.random.randn(len(df)) * 0.1, 0.1, 0.9)
        df["ml_p_short"] = 1.0 - df["ml_p_long"]

    # Use the current GBP config (v4) for diagnostics
    cfg_gbp = cfg.copy()
    # Ensure we run with asset-specific
    bt = EnsembleBacktester(cfg_gbp, asset_key=asset_key)

    print(f"[diag] Running backtest on {len(df)} rows with current GBPUSD config (v4)...")
    trades = bt.run(df.reset_index(drop=True))

    if not trades:
        print("[diag] No trades produced.")
        return

    print(f"[diag] {len(trades)} trades produced")

    # Build per-trade diagnostics frame
    rows = []
    for t in trades:
        entry_ts = t.entry_ts
        exit_ts = t.exit_ts if t.exit_ts is not None else entry_ts
        # MFE / MAE over the trade window (NaN-safe; empty window -> entry price).
        # NOTE: df[...]["high"].max() returns a numpy.float64 SCALAR — wrapping it
        # in max() raised TypeError: 'numpy.float64' object is not iterable.
        window = df[(df["timestamp_utc"] >= entry_ts) & (df["timestamp_utc"] <= exit_ts)]
        if len(window) == 0:
            hi = float(t.entry_price)
            lo = float(t.entry_price)
        else:
            hi = window["high"].max()
            lo = window["low"].min()
            hi = float(hi) if np.isfinite(hi) else float(t.entry_price)
            lo = float(lo) if np.isfinite(lo) else float(t.entry_price)
        if t.direction == 1:
            mfe = max(hi, float(t.entry_price))
            mae = min(lo, float(t.entry_price))
        else:
            mfe = min(lo, float(t.entry_price))
            mae = max(hi, float(t.entry_price))
        rows.append({
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason,
            "pnl": t.pnl,
            "mfe": mfe,
            "mae": mae,
            "tp1_hit": getattr(t, "tp1_hit", None),
            "tp2_hit": getattr(t, "tp2_hit", None),
        })

    tdf = pd.DataFrame(rows)
    os.makedirs("logs", exist_ok=True)
    out_csv = "logs/diag_gbp_profile.csv"
    tdf.to_csv(out_csv, index=False)
    print(f"[diag] Saved raw trades to {out_csv}")

    # Exit reason distribution
    print("\n=== EXIT REASON DISTRIBUTION ===")
    for reason, g in tdf.groupby("exit_reason"):
        print(f"  {reason}: {len(g)} trades, mean_pnl={g['pnl'].mean():.4f}")
    print(f"\nTotal PnL (sample): {tdf['pnl'].sum():.2f}  (n={len(tdf)})")

    # Price of early breakeven: how many BE scratches would have reached TP2/TP3 later
    print("\n=== PRICE OF EARLY BREAKEVEN (would have reached later within horizon?) ===")
    be = tdf[tdf["exit_reason"] == "breakeven"]
    if len(be) == 0:
        print("  no breakeven trades in sample")
    else:
        reached = {"tp1": 0, "tp2": 0, "tp3": 0, "none": 0}
        for _, r in be.iterrows():
            mfe_dist = abs(r["mfe"] - r["entry_price"])
            atr_here = float(df["atr"].median()) if "atr" in df.columns else 0.0015
            if mfe_dist >= 3.0 * atr_here:
                reached["tp3"] += 1
            elif mfe_dist >= 2.0 * atr_here:
                reached["tp2"] += 1
            elif mfe_dist >= 1.0 * atr_here:
                reached["tp1"] += 1
            else:
                reached["none"] += 1
        total = len(be)
        for k in ("tp1", "tp2", "tp3", "none"):
            print(f"  {k}: {reached[k]}/{total} ({100.0 * reached[k] / total:.1f}%)")


if __name__ == "__main__":
    main()
