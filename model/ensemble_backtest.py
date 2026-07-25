"""
Ensemble-aware backtest wrapper: routes the ensemble's bias/confidence through the
existing EventDrivenBacktester execution logic (same signal-at-close(i) ->
entry-at-open(i+1) timing, same spread/slippage/one-position-at-a-time rules).
This is a thin adapter, not a duplicate engine - it reuses backtest/engine.py's
Trade dataclass and exit logic, only replacing WHICH signal drives entries.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, List

from regime.classifier import RegimeLabel
from model.ensemble import compute_ensemble_signal
from backtest.engine import Trade


class EnsembleBacktester:
    """
    Same execution mechanics as EventDrivenBacktester, but the entry signal comes
    from the full ensemble (rule + ML + meta-filter) instead of rule_based_signal alone.
    Requires df to already contain: regime (RegimeLabel), ml_p_long, ml_p_short columns,
    computed causally upstream (regime/classifier.py + model/predictor.py).
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        bt_cfg = cfg["backtest"]
        lab_cfg = cfg["labeling"]
        self.spread = bt_cfg["spread_points"] / 100.0
        self.slippage = bt_cfg["slippage_points"] / 100.0
        self.balance = bt_cfg["initial_balance"]
        self.target_x = lab_cfg["target_pips_x"]
        self.stop_y = lab_cfg["stop_pips_y"]
        self.horizon_n = lab_cfg["horizon_candles_n"]
        self.trades: List[Trade] = []

    def run(self, df: pd.DataFrame) -> List[Trade]:
        n = len(df)
        open_position: Optional[Trade] = None
        pending_direction: Optional[int] = None

        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        timestamps = df["timestamp_utc"].values
        sessions = df["session"].values
        regimes = df["regime"].values
        p_longs = df["ml_p_long"].values
        p_shorts = df["ml_p_short"].values

        for i in range(n):
            if open_position is None and pending_direction is not None and pending_direction != 0:
                direction = pending_direction
                entry_price = opens[i] + (self.spread / 2 if direction == 1 else -self.spread / 2)
                stop_price = entry_price - direction * self.stop_y
                target_price = entry_price + direction * self.target_x
                open_position = Trade(
                    entry_ts=int(timestamps[i]), entry_price=entry_price, direction=direction,
                    stop_price=stop_price, target_price=target_price, session=str(sessions[i]),
                    regime_at_entry=str(regimes[i - 1]) if i > 0 else str(regimes[i]),
                )
                pending_direction = None

            if open_position is not None:
                direction = open_position.direction
                hit_target = (highs[i] >= open_position.target_price) if direction == 1 else (lows[i] <= open_position.target_price)
                hit_stop = (lows[i] <= open_position.stop_price) if direction == 1 else (highs[i] >= open_position.stop_price)
                step = df["timestamp_utc"].diff().mode().iloc[0] if len(df) > 1 else 1
                candles_held = int((timestamps[i] - open_position.entry_ts) / step)

                exit_reason, exit_price = None, None
                if hit_target and hit_stop:
                    exit_reason, exit_price = "stop", open_position.stop_price
                elif hit_stop:
                    exit_reason, exit_price = "stop", open_position.stop_price
                elif hit_target:
                    exit_reason, exit_price = "target", open_position.target_price
                elif candles_held >= self.horizon_n:
                    exit_reason, exit_price = "timeout", closes[i]

                if exit_reason is not None:
                    exit_price += (-self.slippage if direction == 1 else self.slippage)
                    open_position.exit_ts = int(timestamps[i])
                    open_position.exit_price = exit_price
                    open_position.exit_reason = exit_reason
                    open_position.pnl = direction * (exit_price - open_position.entry_price)
                    self.balance += open_position.pnl
                    self.trades.append(open_position)
                    open_position = None

            if open_position is None:
                sig = compute_ensemble_signal(regimes[i], p_longs[i], p_shorts[i], self.cfg)
                pending_direction = {"long": 1, "short": -1, "no_trade": 0}[sig.bias]

        return self.trades
