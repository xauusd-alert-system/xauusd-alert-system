"""
Diagnostic for GBPUSD profile under current FX v3 config (early BE, stop 2.0, H1).
- Loads H1 GBPUSD via scripts.train_mt5.build_full_df (with order-flow).
- Runs EnsembleBacktester with current GBPUSD asset config.
- Outputs to logs/diag_gbp_profile.csv:
  exit_reason distribution + mean PnL
  "price of early BE": for breakeven exits, did price later reach TP1/TP2/TP3 within horizon_n?
  fraction of trades that first went -1/-2 steps (stop-hunt) before going positive.
Also prints summary.

Must work on mock data (no real DB) for tests.
"""

import os
import sys
import pandas as pd
import numpy as np
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, get_signal_grid
from scripts.train_mt5 import build_full_df
from model.ensemble_backtest import EnsembleBacktester
from data.ingestion import fetch_mock_candles  # for smoke tests / no DB


def _make_synthetic_gbp_df(n=2000, price=1.30, atr=0.0015, seed=42):
    """Synthetic H1-like GBPUSD with trend + reversals for diagnostics."""
    np.random.seed(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC")
    # Trend + noise + occasional big moves
    trend = np.linspace(0, 0.08, n) + np.sin(np.arange(n) / 80) * 0.03
    noise = np.cumsum(np.random.randn(n) * 0.0008)
    closes = price + trend + noise
    opens = closes + np.random.randn(n) * 0.0003
    highs = np.maximum(opens, closes) + np.abs(np.random.randn(n)) * 0.0008 + 0.0002
    lows = np.minimum(opens, closes) - np.abs(np.random.randn(n)) * 0.0008 - 0.0002
    df = pd.DataFrame({
        "timestamp_utc": (idx.astype("int64") // 10**9).astype("int64"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.random.randint(1000, 5000, n).astype(float),
        "session": ["london"] * n,
        "atr": atr,
    })
    df["timestamp"] = idx
    return df


def main():
    cfg = load_config()
    asset_key = "GBPUSD"
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    timeframe = asset_cfg.get("timeframe", "H1")

    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    # Try real DB first; fallback to synthetic for tests / when no DB
    try:
        from data.storage import read_candles
        raw = read_candles(db_path, timeframe, asset_key)
        if raw.empty:
            raise ValueError("empty")
        print(f"[diag] Using real DB: {len(raw)} rows for {asset_key} {timeframe}")
        df = build_full_df(
            raw, cfg, db_path=db_path, asset_key=asset_key, timeframe=timeframe
        )
    except Exception as e:
        print(f"[diag] No usable DB or error ({e}); using synthetic mock for GBPUSD profile.")
        raw = _make_synthetic_gbp_df()
        # Minimal features for backtester (ml probs + regime)
        df = build_full_df(raw, cfg, db_path=None, asset_key=asset_key, timeframe=timeframe)
        # Inject plausible ML probs for signal generation in backtester
        df["ml_p_long"] = np.clip(0.5 + (df["close"].diff().fillna(0) > 0).astype(float) * 0.35 + np.random.randn(len(df)) * 0.1, 0.1, 0.9)
        df["ml_p_short"] = 1.0 - df["ml_p_long"]

    # Use the current GBP config (v3) for diagnostics
    cfg_gbp = cfg.copy()
    # Ensure we run with asset-specific
    bt = EnsembleBacktester(cfg_gbp, asset_key=asset_key)

    print(f"[diag] Running backtest on {len(df)} rows with current GBPUSD config (v3)...")
    trades = bt.run(df.reset_index(drop=True))

    if not trades:
        print("[diag] No trades generated on this data slice.")
        return

    trades_df = pd.DataFrame([
        {
            "entry_ts": t.entry_ts,
            "exit_ts": t.exit_ts,
            "direction": t.direction,
            "pnl": t.pnl,
            "exit_reason": t.exit_reason,
            "entry_price": t.entry_price,
            "stop_price": t.stop_price,
            "tp1_price": t.tp1_price,
            "tp2_price": t.tp2_price,
            "tp3_price": t.tp3_price,
        }
        for t in trades
    ])

    os.makedirs("logs", exist_ok=True)
    out_path = "logs/diag_gbp_profile.csv"
    trades_df.to_csv(out_path, index=False)
    print(f"[diag] Saved raw trades to {out_path}")

    # Exit distribution + mean PnL
    exit_counts = Counter(trades_df["exit_reason"])
    print("\n=== EXIT REASON DISTRIBUTION ===")
    for r, c in sorted(exit_counts.items()):
        sub = trades_df[trades_df["exit_reason"] == r]
        print(f"  {r}: {c} trades, mean_pnl={sub['pnl'].mean():.4f}")

    total_pnl = trades_df["pnl"].sum()
    print(f"\nTotal PnL (sample): {total_pnl:.2f}  (n={len(trades_df)})")

    # === "PRICE OF EARLY BE" ===
    # For breakeven exits: would price have reached TP1/TP2/TP3 later within horizon?
    horizon = bt.horizon_n
    be_trades = trades_df[trades_df["exit_reason"] == "breakeven"].copy()
    later_reach = {"tp1": 0, "tp2": 0, "tp3": 0, "none": 0}

    for _, row in be_trades.iterrows():
        # Find the entry index in df (approx by ts)
        entry_idx = df.index[df["timestamp_utc"] == row["entry_ts"]]
        if len(entry_idx) == 0:
            continue
        i0 = int(entry_idx[0])
        i_end = min(len(df), i0 + horizon + 5)
        sub = df.iloc[i0:i_end]

        dir_ = row["direction"]
        tp1 = row["tp1_price"]
        tp2 = row["tp2_price"]
        tp3 = row["tp3_price"]

        hit_tp1 = ((sub["high"] >= tp1).any() if dir_ == 1 else (sub["low"] <= tp1).any())
        hit_tp2 = ((sub["high"] >= tp2).any() if dir_ == 1 else (sub["low"] <= tp2).any())
        hit_tp3 = ((sub["high"] >= tp3).any() if dir_ == 1 else (sub["low"] <= tp3).any())

        if hit_tp3:
            later_reach["tp3"] += 1
        elif hit_tp2:
            later_reach["tp2"] += 1
        elif hit_tp1:
            later_reach["tp1"] += 1
        else:
            later_reach["none"] += 1

    print("\n=== PRICE OF EARLY BREAKEVEN (would have reached later within horizon?) ===")
    n_be = len(be_trades)
    if n_be > 0:
        for k, v in later_reach.items():
            pct = 100 * v / n_be
            print(f"  {k}: {v}/{n_be} ({pct:.1f}%)")
    else:
        print("  (no breakeven exits in this run)")

    # === Stop-hunt before positive ===
    # Trades that initially went against (hit -1 or -2 steps) before eventual positive outcome.
    hunt_count = 0
    for t in trades:
        if t.exit_reason in ("tp3_runner", "timeout") and t.pnl > 0:
            # crude heuristic: if entry to min excursion went below entry -1*step equiv
            step = abs(t.tp1_price - t.entry_price)
            if t.direction == 1:
                min_after = min(df[(df["timestamp_utc"] >= t.entry_ts) & (df["timestamp_utc"] <= t.exit_ts)]["low"].min() or t.entry_price)
                if min_after < t.entry_price - 1.0 * step:
                    hunt_count += 1
            else:
                max_after = max(df[(df["timestamp_utc"] >= t.entry_ts) & (df["timestamp_utc"] <= t.exit_ts)]["high"].max() or t.entry_price)
                if max_after > t.entry_price + 1.0 * step:
                    hunt_count += 1

    hunt_frac = hunt_count / max(1, len(trades))
    print(f"\n=== STOP-HUNT BEFORE POSITIVE ===")
    print(f"  Trades that dipped -1/-2 step before final + outcome: {hunt_count}/{len(trades)} ({hunt_frac*100:.1f}%)")

    print("\n[diag] Done. See logs/diag_gbp_profile.csv")


if __name__ == "__main__":
    main()
