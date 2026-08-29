"""Задача 3.3: CUSUM risk-adjusted sanity (P2-критерий из preregistration).

Фича несёт режимную информацию, если сегменты сразу после change-point
(cp_bars_since <= 24) отличаются по риск-профилю от остальных баров.

Метрики на XAUUSD M15 (до cutoff): forward 12-барная доходность (окно
labeling-горизонта) и realised vol — по группам баров.

Research-only. Никаких prod-эффектов.
"""

import sys as _sys

if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from config.loader import load_config  # noqa: E402
from scripts.run_backtest import build_full_df, load_asset_history, truncate_before  # noqa: E402


def main() -> None:
    cfg = load_config()
    asset = "XAUUSD"
    raw = load_asset_history("data/market_data_mt5.sqlite", "M15", asset)
    raw = truncate_before(raw, "2026-08-08", asset)
    df = build_full_df(cfg, raw, db_path="data/market_data_mt5.sqlite", asset_key=asset)

    fwd = 12  # labeling horizon (traded event на M15 ~ 12 баров)
    log_ret = np.log(df["close"] / df["close"].shift(1))
    fwd_ret = df["close"].shift(-fwd) / df["close"] - 1.0
    realized_vol = log_ret.rolling(fwd).std().shift(-fwd)

    cp = df["cp_bars_since"]
    post_cp = (cp <= 24) & cp.notna()
    other = ~post_cp & cp.notna()

    groups = {
        "post_cp (<=24)": post_cp,
        "other": other,
    }
    print(f"Bars total={len(df)}, post_cp={post_cp.sum()}, other={other.sum()}")
    rows = []
    for name, mask in groups.items():
        fr = fwd_ret[mask].dropna()
        rv = realized_vol[mask].dropna()
        rows.append(
            {
                "group": name,
                "n_bars": int(mask.sum()),
                "fwd_ret_mean_bp": float(fr.mean() * 1e4),
                "fwd_ret_std_bp": float(fr.std() * 1e4),
                "ret_over_vol": float(fr.mean() / rv.mean()) if rv.mean() else float("nan"),
                "realized_vol_bp": float(rv.mean() * 1e4),
                "fwd_ret_p90_bp": float(fr.quantile(0.9) * 1e4),
                "fwd_ret_p10_bp": float(fr.quantile(0.1) * 1e4),
            }
        )
    out = pd.DataFrame(rows)
    print(out.to_string(index=False))

    # CP-direction sanity: mean forward return после up-CP vs down-CP.
    sign = df["cp_last_sign"]
    fresh_up = post_cp & (sign == 1)
    fresh_down = post_cp & (sign == -1)
    for name, mask in (("fresh_up", fresh_up), ("fresh_down", fresh_down)):
        fr = fwd_ret[mask].dropna()
        print(f"{name}: n={int(mask.sum())}, fwd_ret_mean_bp={fr.mean()*1e4:.2f}" if len(fr) else f"{name}: n=0")

    out.to_csv("results/xauusd_cusum_sanity.csv", index=False)
    print("-> results/xauusd_cusum_sanity.csv")


if __name__ == "__main__":
    main()
