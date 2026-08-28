"""
Portfolio report (quant audit action 5): build the daily net-R matrix from the
walk-forward per-asset trades, compute strategy correlations, ENB, cluster
risk parity weights, scheme comparison (with/without XAG), and kill-switch
thresholds from the backtest distribution.

Data sources (priority):
1. per-asset per-trade CSVs `logs/diag_r_metrics_<asset>.csv` (if present);
2. otherwise runs the honest walk-forward for the requested assets itself.

Usage:
    python -m scripts.portfolio --assets XAUUSD,BTCUSD,EURUSD,GBPUSD
    python -m scripts.portfolio --assets XAUUSD,BTCUSD,EURUSD,GBPUSD,XAGUSD --max-folds 8
"""

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.portfolio import (
    cluster_risk_parity_weights,
    compare_schemes,
    daily_r_matrix,
    effective_number_bets,
    kill_switch_thresholds,
    strategy_correlation,
)
from config.loader import load_config

DEFAULT_CLUSTERS = {"metals": ["XAUUSD", "XAGUSD"],
                    "fx": ["EURUSD", "GBPUSD"],
                    "crypto": ["BTCUSD"]}


def load_trades_for_asset(asset_key: str, cfg: dict, max_folds: int | None) -> pd.DataFrame:
    """Per-trade frame with entry_ts + net_r: from the diag CSV cache or a
    fresh honest walk-forward run."""
    csv_path = f"logs/diag_r_metrics_{asset_key.lower()}.csv"
    if os.path.exists(csv_path):
        tdf = pd.read_csv(csv_path)
        if {"entry_ts", "net_r"}.issubset(tdf.columns) and len(tdf):
            return tdf
    from model.ensemble_backtest import EnsembleBacktester
    from scripts.deflated_sharpe import (
        _SYNTH_DEFAULTS,
        _build_fold_frames,
        _inject_biased_probs,
        _make_synthetic_wf_df,
    )
    from scripts.run_backtest import merge_asset_cfg

    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    timeframe = asset_cfg.get("timeframe") or "M5"
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    try:
        from scripts.run_backtest import build_full_df, load_asset_history
        raw = load_asset_history(db_path, timeframe, asset_key)
        df = build_full_df(cfg, raw, db_path=db_path, asset_key=asset_key)
    except Exception:
        spec = _SYNTH_DEFAULTS.get(asset_key, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)
    windows, frames = _build_fold_frames(df, cfg, asset_key, max_folds)
    bt_cfg = cfg.get("backtest", {})
    volume = float(bt_cfg.get("volume", 0.01))
    pvl = float(asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0)))
    rows = []
    for fdf in frames:
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        for t in engine.run(fdf.reset_index(drop=True)):
            risk = abs(t.entry_price - t.initial_stop_price) * t.volume * pvl \
                if t.initial_stop_price else 0.0
            rows.append({"entry_ts": int(t.entry_ts),
                         "net_r": float(t.pnl / risk) if risk > 1e-12 else float("nan")})
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Portfolio analytics (daily R, ENB, risk parity, kill-switch).")
    parser.add_argument("--assets", default="XAUUSD,BTCUSD,EURUSD,GBPUSD")
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/portfolio.json)")
    args = parser.parse_args(argv)

    cfg = load_config()
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    trades = {a: load_trades_for_asset(a, cfg, args.max_folds) for a in assets}
    daily = daily_r_matrix(trades)
    if daily.empty:
        raise SystemExit("[portfolio] no trade data for the requested assets.")

    print(f"\n=== Portfolio (daily net R): {len(daily)} days, {list(daily.columns)} ===")
    print("Correlation of strategy daily R:")
    print(strategy_correlation(daily).round(3).to_string())
    print(f"ENB (all assets) = {effective_number_bets(daily):.2f}")

    schemes = compare_schemes(daily, DEFAULT_CLUSTERS)
    print("\nScheme comparison:")
    for name, m in schemes.items():
        print(f"  {name:<20} Sharpe={m['sharpe']}  maxDD={m['max_dd_r']}R  "
              f"Calmar={m['calmar']}  ENB={m['enb']}")

    crp = cluster_risk_parity_weights(list(daily.columns), DEFAULT_CLUSTERS)
    print("\nCluster risk parity weights:", crp.round(4).to_dict())

    no_xag = [a for a in assets if a != "XAGUSD"]
    if len(no_xag) != len(assets) and len(no_xag) >= 1:
        daily_no = daily[no_xag]
        schemes_no = compare_schemes(daily_no, DEFAULT_CLUSTERS)
        crp_no = cluster_risk_parity_weights(no_xag, DEFAULT_CLUSTERS)
        print(f"\nWITHOUT XAG ({len(no_xag)} assets):")
        print(f"  ENB = {effective_number_bets(daily_no):.2f} | cluster parity "
              f"Sharpe = {schemes_no['cluster_risk_parity']['sharpe']} "
              f"vs with-XAG Sharpe = {schemes['cluster_risk_parity']['sharpe']}")
        print("  weights:", crp_no.round(4).to_dict())

    ks = kill_switch_thresholds(daily)
    print("\nKill-switch thresholds (from backtest distribution):")
    print(f"  daily 2-sigma = {ks['daily_2sigma']}R | weekly 3-sigma = "
          f"{ks['weekly_3sigma']}R (n_days={ks['n_days']})")

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or "logs/portfolio.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"correlation": strategy_correlation(daily).round(4).to_dict(),
                   "enb": effective_number_bets(daily),
                   "schemes": schemes,
                   "cluster_weights": crp.to_dict(),
                   "kill_switch": ks}, f, indent=2, default=str)
    print(f"\n[portfolio] -> {out_json}")


if __name__ == "__main__":
    main()
