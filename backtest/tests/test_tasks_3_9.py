import pandas as pd
import pytest

from backtest.metrics import pnl_concentration_report, required_auc_for_pf_target


def test_task3_pnl_concentration_report():
    # Synthetic trades with known concentration:
    # 20 trades total, top 5 make +500, other 15 make small PnLs summing to +100
    pnls = [150.0, 120.0, 100.0, 80.0, 50.0] + [10.0, -5.0, 15.0, -10.0, 5.0] * 3
    folds = ["fold_1"] * 5 + ["fold_2"] * 5 + ["fold_3"] * 5 + ["fold_4"] * 5
    dates = pd.date_range("2024-01-01", periods=20, freq="D")

    df = pd.DataFrame(
        {
            "pnl": pnls,
            "fold_id": folds,
            "date": dates.astype(str),
        }
    )

    rep = pnl_concentration_report(df, top5_threshold=0.35, fold_threshold=0.30)

    assert "top5_share" in rep
    assert "best_fold_share" in rep
    assert "worst_day_pnl" in rep
    assert "top5_flag" in rep
    assert "best_fold_flag" in rep

    # Top 5 trades = 500 / 580 ~ 86% -> flag must be True
    assert rep["top5_share"] > 0.35
    assert rep["top5_flag"] is True
    assert rep["has_red_flags"] is True

    # Best fold is fold_1 (500) -> fold_1 share ~ 500 / 535 > 0.30
    assert rep["best_fold_id"] == "fold_1"
    assert rep["best_fold_flag"] is True

    # Worst day
    assert rep["worst_day_pnl"] == -10.0


def test_task9_required_auc_benchmark():
    """Unit test Task 9: reproduces the benchmark from the audit document:
    PF 1.07 -> 1.21 at AUC ~ 0.55 with 40% cutoff (N=1000 -> 600)."""
    res = required_auc_for_pf_target(
        pf_current=1.07,
        pf_target=1.21,
        win_rate=0.517,
        avg_win_r=1.0,
        sigma_r=0.40,
        cutoff_fraction=0.40,
    )

    assert res["required_auc"] == pytest.approx(0.55, abs=0.04)
    assert res["realistic"] is True
    assert res["verdict"] == "realistic"
