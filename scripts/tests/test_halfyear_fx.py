import copy

import pandas as pd

from scripts.fetch_halfyear_fx import (
    EXIT_VARIANTS,
    apply_exit_variant,
    make_windows,
    six_month_start,
    summarize_trades,
)


def test_six_month_windows_start_at_requested_date_and_cover_cutoff():
    start = six_month_start("2026-08-08")
    windows = make_windows(start, "2026-08-08", train_days=300, test_days=30, step_days=30)

    assert windows[0].test_start_ts == int(pd.Timestamp(start).timestamp())
    assert windows[0].train_end_ts == windows[0].test_start_ts
    assert windows[0].train_start_ts == windows[0].test_start_ts - 300 * 86400
    assert windows[-1].test_end_ts == int(pd.Timestamp("2026-08-08", tz="UTC").timestamp())


def test_exit_variant_isolated_and_models_full_position_to_fixed_target():
    cfg = {
        "assets": {
            "XAUUSD": {
                "signal_grid": {
                    "tp1_mult": 1.0,
                    "tp2_mult": 2.0,
                    "tp3_mult": 3.0,
                    "stop_mult": 2.0,
                }
            }
        }
    }
    original = copy.deepcopy(cfg)
    variant = apply_exit_variant(cfg, "XAUUSD", "rr_3.5")

    assert cfg == original
    grid = variant["assets"]["XAUUSD"]["signal_grid"]
    assert (grid["tp1_mult"], grid["tp2_mult"], grid["tp3_mult"]) == (3.5, 3.5, 3.5)
    assert grid["stop_mult"] == 1.0
    assert grid["scaleout"] == {"tp1_ratio": 0.0, "tp2_ratio": 0.0}
    assert grid["breakeven_trigger_atr"] > 10


def test_summary_reports_money_r_and_quarter_breakdown():
    trades = pd.DataFrame(
        [
            {
                "entry_ts": int(pd.Timestamp("2026-02-10", tz="UTC").timestamp()),
                "exit_ts": int(pd.Timestamp("2026-02-10 01:00", tz="UTC").timestamp()),
                "exit_reason": "target",
                "pnl": 3.0,
                "entry_price": 100.0,
                "initial_stop_price": 99.0,
                "tp1_price": 103.5,
                "volume": 1.0,
            },
            {
                "entry_ts": int(pd.Timestamp("2026-05-10", tz="UTC").timestamp()),
                "exit_ts": int(pd.Timestamp("2026-05-10 01:00", tz="UTC").timestamp()),
                "exit_reason": "stop",
                "pnl": -1.0,
                "entry_price": 100.0,
                "initial_stop_price": 99.0,
                "tp1_price": 100.85,
                "volume": 1.0,
            },
        ]
    )
    summary = summarize_trades(trades, point_value_lot=1.0, volume=1.0)

    assert summary["n_trades"] == 2
    assert summary["win_rate_pct"] == 50.0
    assert summary["profit_factor"] == 3.0
    assert summary["total_pnl"] == 2.0
    assert summary["r_metrics"]["mean_r"] == 1.0
    assert set(summary["quarters"]) == {"2026-Q1", "2026-Q2"}
    assert summary["exit_reasons"] == {"target": 1, "stop": 1}
