import pandas as pd

from backtest.plateau import select_plateau_config


def test_task5_select_plateau_config_prefers_plateau_over_sharp_peak():
    """Unit test Task 5: on a synthetic grid with one sharp isolated peak
    and one wide plateau, select_plateau_config selects the plateau center
    rather than the global argmax."""
    # 2D grid: 7 x 7
    stop_mults = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    tp_mults = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    param_grid = {
        "stop_mult": stop_mults,
        "tp_mult": tp_mults,
    }

    rows = []
    for s_idx, s in enumerate(stop_mults):
        for t_idx, t in enumerate(tp_mults):
            # 1. Sharp isolated peak at (0, 0)
            if s_idx == 0 and t_idx == 0:
                metric = 1000.0  # huge spike
            elif s_idx <= 1 and t_idx <= 1:
                metric = -50.0  # negative neighbors surrounding the peak

            # 2. Wide plateau around (4, 4): indices 2, 3, 4, 5, 6
            elif 2 <= s_idx <= 6 and 2 <= t_idx <= 6:
                # All points in this 5x5 region are positive
                metric = 50.0 + 5.0 * (s_idx == 4 and t_idx == 4)
            else:
                metric = -10.0  # background is negative

            rows.append(
                {
                    "stop_mult": s,
                    "tp_mult": t,
                    "oos_metric": metric,
                }
            )

    results_df = pd.DataFrame(rows)

    # Standard argmax would choose (0, 0) -> stop_mult=1.0, tp_mult=1.0
    argmax_row = results_df.loc[results_df["oos_metric"].idxmax()]
    assert argmax_row["stop_mult"] == 1.0
    assert argmax_row["tp_mult"] == 1.0

    # select_plateau_config MUST choose the center of the wide plateau at (4, 4) -> 5.0, 5.0
    chosen = select_plateau_config(results_df, param_grid, metric_col="oos_metric", min_metric=0.0)

    assert chosen["stop_mult"] == 5.0
    assert chosen["tp_mult"] == 5.0
    assert chosen["_selection_type"] == "plateau_center"
    assert chosen["_plateau_size"] >= 8
