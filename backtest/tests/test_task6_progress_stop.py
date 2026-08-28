import pandas as pd

from backtest.engine import EventDrivenBacktester, Trade
from backtest.metrics import progress_pnl_curve
from regime.classifier import RegimeLabel


def _make_flat_df(n=50, price=1.2800, atr=0.0010):
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    ts = [int(t.timestamp()) for t in idx]
    return pd.DataFrame({
        "timestamp_utc": ts,
        "open": price,
        "high": price + 0.0001,
        "low": price - 0.0001,
        "close": price,
        "volume": 100.0,
        "session": "london",
        "regime": RegimeLabel.TREND_UP,
        "atr": atr,
        "ml_p_long": 0.90,
        "ml_p_short": 0.10,
    })


def test_task6_progress_stop_triggers_on_flat_price():
    """Unit test Task 6: when price fails to make 0.3x ATR progress in 0.5x horizon,
    progress-stop exits the trade early."""
    cfg = {
        "backtest": {
            "spread_points": 0, "slippage_points": 0, "initial_balance": 100.0,
            "risk_per_trade_pct": 2.0, "volume": 0.01,
        },
        "labeling": {"method": "atr_scaled", "horizon_candles_n": 20, "atr_column": "atr"},
        "signal_grid": {
            "tp1_mult": 2.0, "tp2_mult": 3.0, "tp3_mult": 4.0, "stop_mult": 3.0,
            "breakeven_trigger_atr": 1.0,
            "progress_stop_enabled": True,
            "progress_stop_ratio": 0.5,  # 10 bars
            "progress_stop_atr": 0.3,
        },
    }

    bt = EventDrivenBacktester(cfg)
    df = _make_flat_df(n=40, price=1.2800, atr=0.0010)
    trades = bt.run(df)

    assert len(trades) >= 1
    assert trades[0].exit_reason == "progress_stop"


def test_task6_progress_stop_does_not_trigger_when_progress_achieved():
    """Unit test Task 6: when price makes strong progress in our favor,
    progress-stop does not trigger and trade hits target/breakeven."""
    cfg = {
        "backtest": {
            "spread_points": 0, "slippage_points": 0, "initial_balance": 100.0,
            "risk_per_trade_pct": 2.0, "volume": 0.01,
        },
        "labeling": {"method": "atr_scaled", "horizon_candles_n": 20, "atr_column": "atr"},
        "signal_grid": {
            "tp1_mult": 2.0, "tp2_mult": 3.0, "tp3_mult": 4.0, "stop_mult": 3.0,
            "breakeven_trigger_atr": 1.0,
            "progress_stop_enabled": True,
            "progress_stop_ratio": 0.5,  # 10 bars
            "progress_stop_atr": 0.3,
        },
    }

    bt = EventDrivenBacktester(cfg)
    df = _make_flat_df(n=40, price=1.2800, atr=0.0010)
    # Give strong upward move on bar 5 (0.8x ATR progress)
    df.loc[5:, "high"] = 1.2800 + 0.0008
    df.loc[5:, "close"] = 1.2800 + 0.0008

    trades = bt.run(df)
    assert len(trades) >= 1
    # Should not exit on progress_stop (exits on timeout or target)
    assert trades[0].exit_reason != "progress_stop"


def test_task6_holding_bar_pnl_curve():
    """Unit test Task 6: diagnostic curve aggregates PnL by holding bar."""
    df = _make_flat_df(n=30)
    trade = Trade(
        entry_ts=int(df["timestamp_utc"].iloc[0]),
        entry_price=1.2800,
        direction=1,
        stop_price=1.2770,
        target_price=1.2830,
        session="london",
        regime_at_entry="trend_up",
        exit_ts=int(df["timestamp_utc"].iloc[15]),
        exit_price=1.2800,
        exit_reason="progress_stop",
        pnl=0.0,
    )
    curve = progress_pnl_curve(df, [trade], max_bars=20)
    assert len(curve) == 20
    assert "mean_pnl" in curve.columns
    assert curve.loc[curve["bar"] == 5, "n_open"].iloc[0] == 1
    assert curve.loc[curve["bar"] == 18, "n_open"].iloc[0] == 0
