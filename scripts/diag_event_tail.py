"""
Tail-loss concentration around Tier-1 events (quant audit 2026-08-07, Claude
plan question 3, "check in one hour"):

    "Возьми худшие 5% сделок по R и построй гистограмму по «минутам
     до/после события Tier-1». Если больше 30% хвостовых убытков кучкуются
     в ±30 минут вокруг Tier-1 — жёсткий блок окупит себя немедленно."

Implementation:
- trades from the honest walk-forward (same engine);
- events from data/news_filter.fetch_economic_calendar() (ForexFactory High /
  USD/ALL), OR a local CSV (--calendar path with epoch seconds), OR a
  synthetic calendar (--synthetic-calendar, tests/demo only);
- for the worst pct% (default 5) trades by net R, compute minutes from entry
  to the NEAREST event and bucket them: [-inf,-120), [-120,-60), [-60,-30),
  [-30,30], (30,60], (60,120], (120,inf];
- report the share of tail losses inside ±30 min and the audit verdict.

Usage:
    python -m scripts.diag_event_tail --asset GBPUSD
    python -m scripts.diag_event_tail --asset XAUUSD --calendar events.csv
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import (
    _make_synthetic_wf_df,
    _inject_biased_probs,
    _build_fold_frames,
    _SYNTH_DEFAULTS,
)
from scripts.run_backtest import merge_asset_cfg
from model.ensemble_backtest import EnsembleBacktester

BUCKETS = [
    ("<= -120m", lambda m: m <= -120),
    ("-120..-60m", lambda m: -120 < m <= -60),
    ("-60..-30m", lambda m: -60 < m <= -30),
    ("±30m", lambda m: -30 < m <= 30),
    ("+30..+60m", lambda m: 30 < m <= 60),
    ("+60..+120m", lambda m: 60 < m <= 120),
    ("> +120m", lambda m: m > 120),
]


def load_events(db_events=None, calendar_path: str | None = None,
                synthetic: bool = False, seed: int = 0) -> list[dict]:
    """Events as [{timestamp_utc, title}]. Priority: synthetic > CSV > live API."""
    if synthetic:
        rng = np.random.default_rng(seed)
        base = 1_700_000_000
        return [{"timestamp_utc": int(base + rng.integers(0, 400 * 86400)),
                 "title": f"Synthetic event {i}"} for i in range(200)]
    if calendar_path and os.path.exists(calendar_path):
        df = pd.read_csv(calendar_path)
        return [{"timestamp_utc": int(ts), "title": str(row.get("title", "event"))}
                for ts, row in zip(df["timestamp_utc"], df.iterrows())]
    events = db_events or []
    if not events:
        from data.news_filter import fetch_economic_calendar
        events = fetch_economic_calendar()
    return events


def _minutes_to_nearest(event_ts: np.ndarray, entry_ts: int) -> float:
    if len(event_ts) == 0:
        return float("inf")
    i = int(np.searchsorted(event_ts, entry_ts))
    cands = []
    if i < len(event_ts):
        cands.append(event_ts[i])
    if i > 0:
        cands.append(event_ts[i - 1])
    return float(min(abs(c - entry_ts) for c in cands) / 60.0)


def run_event_tail(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                   events: list[dict], tail_pct: float = 5.0,
                   max_folds: int | None = None) -> dict:
    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(f"No walk-forward folds produced for {asset_key}.")

    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    bt_cfg = cfg.get("backtest", {})
    volume = float(bt_cfg.get("volume", 0.01))
    point_value_lot = float(asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0)))
    event_ts = np.asarray(sorted(e["timestamp_utc"] for e in events), dtype=np.int64)

    rows = []
    for fdf in frames:
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        for t in engine.run(fdf.reset_index(drop=True)):
            risk = abs(t.entry_price - t.initial_stop_price) * t.volume * point_value_lot \
                if t.initial_stop_price else 0.0
            net_r = float(t.pnl / risk) if risk > 1e-12 else float("nan")
            rows.append({"entry_ts": int(t.entry_ts), "net_r": net_r,
                         "exit_reason": t.exit_reason})

    if len(rows) < 10:
        return {"asset": asset_key, "n_trades": len(rows), "n_events": len(events),
                "verdict": "insufficient trades", "buckets": []}

    tdf = pd.DataFrame(rows)
    tdf = tdf[tdf["net_r"].notna()]
    thr = float(tdf["net_r"].quantile(tail_pct / 100.0))
    tail = tdf[tdf["net_r"] <= thr]
    non_tail = tdf[tdf["net_r"] > thr]
    tdf["minutes_to_event"] = tdf["entry_ts"].apply(
        lambda ts: _minutes_to_nearest(event_ts, int(ts)))
    tail = tail.copy()
    tail["minutes_to_event"] = tail["entry_ts"].apply(
        lambda ts: _minutes_to_nearest(event_ts, int(ts)))
    non_tail = non_tail.copy()
    non_tail["minutes_to_event"] = non_tail["entry_ts"].apply(
        lambda ts: _minutes_to_nearest(event_ts, int(ts)))

    def bucket_table(d: pd.DataFrame) -> list[dict]:
        out = []
        for label, pred in BUCKETS:
            if len(d) == 0:
                out.append({"bucket": label, "n": 0, "share_pct": 0.0})
                continue
            sel = d[d["minutes_to_event"].apply(pred)]
            out.append({"bucket": label, "n": int(len(sel)),
                        "share_pct": round(100.0 * len(sel) / len(d), 1)})
        return out

    tail_buckets = bucket_table(tail)
    all_buckets = bucket_table(tdf)
    in_30_tail = sum(b["n"] for b in tail_buckets if b["bucket"] == "±30m")
    in_30_all = sum(b["n"] for b in all_buckets if b["bucket"] == "±30m")
    tail_share_in_30 = 100.0 * in_30_tail / max(len(tail), 1)
    all_share_in_30 = 100.0 * in_30_all / max(len(tdf), 1)

    if tail_share_in_30 >= 30.0:
        verdict = ("HARD BLOCK justified: >30% of tail losses cluster inside "
                   "±30 min around Tier-1 events")
    elif tail_share_in_30 > all_share_in_30 * 1.5:
        verdict = "elevated tail clustering: consider a pre-event block"
    else:
        verdict = "no meaningful tail clustering around Tier-1 events"

    return {"asset": asset_key, "n_trades": int(len(tdf)), "n_events": len(events),
            "tail_pct": tail_pct, "tail_threshold_r": round(thr, 4),
            "n_tail": int(len(tail)), "tail_share_in_30m_pct": round(tail_share_in_30, 1),
            "all_share_in_30m_pct": round(all_share_in_30, 1),
            "tail_buckets": tail_buckets, "all_buckets": all_buckets,
            "verdict": verdict}


def print_report(d: dict) -> None:
    print(f"\n=== Tail losses around Tier-1 events: {d['asset']} ===")
    print(f"Trades: {d['n_trades']} | events: {d['n_events']} | tail = worst "
          f"{d['tail_pct']}% (netR <= {d['tail_threshold_r']}, n={d['n_tail']})")
    if d["n_trades"] < 10:
        print("  insufficient trades.")
        return
    print(f"Tail share inside ±30m: {d['tail_share_in_30m_pct']}% vs all trades "
          f"{d['all_share_in_30m_pct']}%")
    print("Tail buckets (minutes from entry to nearest event):")
    for b in d["tail_buckets"]:
        print(f"  {b['bucket']:<12} n={b['n']:<6} share={b['share_pct']}%")
    print(f"Verdict: {d['verdict']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Tail-loss concentration around Tier-1 events.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--tail-pct", type=float, default=5.0)
    parser.add_argument("--calendar", default=None, help="CSV with timestamp_utc column")
    parser.add_argument("--synthetic-calendar", action="store_true",
                        help="Use a synthetic event calendar (demo/tests only)")
    parser.add_argument("--out", default=None, help="JSON output (default: logs/event_tail_<asset>.json)")
    args = parser.parse_args(argv)

    cfg = load_config()
    assets = cfg.get("assets", {})
    if args.asset not in assets:
        raise SystemExit(f"Unknown asset: {args.asset}")
    asset_cfg = assets[args.asset]
    timeframe = args.timeframe or asset_cfg.get("timeframe") or "M5"
    db_path = args.db_path or cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    synthetic = False
    try:
        from scripts.run_backtest import load_asset_history, build_full_df
        raw = load_asset_history(db_path, timeframe, args.asset)
        df = build_full_df(cfg, raw, db_path=db_path, asset_key=args.asset)
        print(f"[event] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[event] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    events = load_events(calendar_path=args.calendar,
                         synthetic=args.synthetic_calendar or synthetic)
    if not events:
        print("[event] WARNING: no calendar available (API offline / no --calendar). "
              "Use --calendar events.csv or --synthetic-calendar for a demo run.")
        return

    d = run_event_tail(cfg, args.asset, df, events, tail_pct=args.tail_pct,
                       max_folds=args.max_folds)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or f"logs/event_tail_{args.asset.lower()}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[event] -> {out_json}")


if __name__ == "__main__":
    main()
