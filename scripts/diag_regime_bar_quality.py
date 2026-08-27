"""Bar-level regime classifier quality across all 5 assets.

Measures whether the ADX/DI rule-based regime label carries ANY predictive
power for forward returns at the bar level (not confounded by the trade-level
direction imbalance where BTC is ~100% short / XAU ~100% long).

For each asset (production timeframe):
  - per-regime forward return (1/5/10 bars), win rates
  - regime autocorrelation / stickiness (P(label stays same next bar))
  - ADX/DI correlation with forward return
  - "aligned-trend" strategy test: long when trend_up, short when trend_down,
    flat in range/compression -> per-bar meanR vs baseline

Usage:
    python -m scripts.diag_regime_bar_quality
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config.loader import load_config, resolve_asset_timeframe
from scripts.run_backtest import load_asset_history, build_full_df
from regime.classifier import classify_regime_series, RegimeLabel

# production timeframe per asset (asset override -> market_data.timeframe)
ASSET_TF = {
    "XAUUSD": "M15",
    "BTCUSD": "M5",
    "EURUSD": "H1",
    "GBPUSD": "H1",
    "XAGUSD": "M15",
}


def _tf_for(cfg: dict, asset: str) -> str:
    return resolve_asset_timeframe(cfg, asset)


def _regime_str(v) -> str:
    """RegimeLabel enum -> 'trend_up' etc. (str() on the pandas StringArray is
    unreliable, so go through .value when present)."""
    return v.value if hasattr(v, "value") else str(v)


def regime_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for reg, g in df.groupby("regime", observed=True):
        if _regime_str(reg) in ("no_trade", ""):
            continue
        rows.append({
            "regime": _regime_str(reg),
            "n": len(g),
            "share_pct": 100 * len(g) / len(df),
            "fwd1_mean": g["fwd1"].mean(),
            "fwd1_wr": 100 * (g["fwd1"] > 0).mean(),
            "fwd5_mean": g["fwd5"].mean(),
            "fwd5_wr": 100 * (g["fwd5"] > 0).mean(),
            "fwd10_mean": g["fwd10"].mean(),
            "fwd10_wr": 100 * (g["fwd10"] > 0).mean(),
        })
    return pd.DataFrame(rows).sort_values("n", ascending=False)


def stickiness(df: pd.DataFrame) -> float:
    """Fraction of bars where regime == previous bar's regime."""
    reg = df["regime"].map(_regime_str)
    same = (reg == reg.shift(1)).mean()
    return float(same)


def trend_strategy(df: pd.DataFrame) -> dict:
    """Long when trend_up, short when trend_down, flat otherwise."""
    reg = df["regime"].map(_regime_str)
    pos = np.where(reg == "trend_up", 1.0, np.where(reg == "trend_down", -1.0, 0.0))
    ret = pd.Series(pos, index=df.index) * df["fwd1"]
    active = pos != 0
    return {
        "active_share": float(active.mean()),
        "active_meanR": float(ret[active].mean()) if active.any() else np.nan,
        "active_wr": float((ret[active] > 0).mean()) if active.any() else np.nan,
        "all_meanR": float(df["fwd1"].mean()),
        "all_wr": float((df["fwd1"] > 0).mean()),
        "up_long_meanR": float(df.loc[reg == "trend_up", "fwd1"].mean()) if (reg == "trend_up").any() else np.nan,
        "down_short_meanR": float(df.loc[reg == "trend_down", "fwd1"].mean()) if (reg == "trend_down").any() else np.nan,
    }


def main() -> None:
    cfg = load_config()
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    print("=" * 96)
    print("BAR-LEVEL REGIME CLASSIFIER QUALITY (production timeframes)")
    print("=" * 96)

    summary = []
    for asset, tf in ASSET_TF.items():
        prod_tf = _tf_for(cfg, asset)
        use_tf = prod_tf or tf
        try:
            raw = load_asset_history(db_path, use_tf, asset)
        except Exception as e:  # noqa: BLE001
            print(f"\n[{asset}] LOAD FAILED ({use_tf}): {e}")
            continue
        if len(raw) < 300:
            print(f"\n[{asset}] only {len(raw)} bars on {use_tf} - skip")
            continue
        df = build_full_df(cfg, raw, db_path, asset)
        df = df.reset_index(drop=True)
        close = df["close"]
        for h in (1, 5, 10):
            df[f"fwd{h}"] = close.shift(-h) / close - 1.0
        # drop rows without regime or forward return
        df = df.dropna(subset=["regime", "fwd1"])

        st = stickiness(df)
        ts = trend_strategy(df)
        print(f"\n{'#' * 90}")
        print(f"# {asset}  TF={use_tf}  bars={len(df)}  stickiness={st:.3f}")
        print(f"{'#' * 90}")
        print("\n-- Forward return by regime --")
        rs = regime_stats(df)
        if len(rs):
            print(rs.round(4).to_string(index=False))
        print("\n-- Trend-following strategy (long in trend_up, short in trend_down) --")
        for k, v in ts.items():
            print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
        print("\n-- ADX/DI correlation with forward return --")
        for col in ("adx", "plus_di", "minus_di"):
            if col in df.columns:
                c1 = df[col].corr(df["fwd1"])
                c5 = df[col].corr(df["fwd5"])
                print(f"  {col}: corr(fwd1)={c1:+.4f}  corr(fwd5)={c5:+.4f}")

        # which regime has best forward return?
        if len(rs):
            best = rs.loc[rs["fwd1_mean"].idxmax()]
            worst = rs.loc[rs["fwd1_mean"].idxmin()]
            summary.append({
                "asset": asset, "tf": use_tf, "bars": len(df),
                "stickiness": st,
                "best_regime": best["regime"], "best_fwd1": best["fwd1_mean"],
                "worst_regime": worst["regime"], "worst_fwd1": worst["fwd1_mean"],
                "active_share": ts["active_share"], "active_meanR": ts["active_meanR"],
                "all_meanR": ts["all_meanR"],
            })

    if summary:
        print("\n" + "=" * 96)
        print("SUMMARY")
        print("=" * 96)
        s = pd.DataFrame(summary).round(4)
        print(s.to_string(index=False))


if __name__ == "__main__":
    main()
