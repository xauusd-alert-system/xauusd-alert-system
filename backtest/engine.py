"""
Event-driven backtester for the rule-based baseline (and later, ML/ensemble) signals.

CRITICAL DESIGN DECISION - avoiding look-ahead in execution timing:
A signal decided from information available AT THE CLOSE of candle i can only be
acted upon starting at candle i+1 (we cannot retroactively fill an order inside the
candle whose close just produced the signal). This engine enforces that gap
explicitly: signal_at_close(i) -> entry_at_open(i+1). This is the second most common
source of backtest look-ahead bias after feature leakage, so it is isolated here.

The engine processes candles strictly in order (no vectorized shortcuts), maintains
at most ONE open position at a time (consistent with small-deposit, no-leverage-blowup
risk context), and applies spread/slippage on both entry and exit.
"""
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from config.loader import get_signal_grid, resolve_signal_step
from regime.classifier import RegimeLabel


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    direction: int  # +1 long, -1 short
    stop_price: float
    target_price: float
    session: str
    regime_at_entry: str
    exit_ts: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "target", "stop", "timeout"
    pnl: Optional[float] = None


def rule_based_signal(regime: RegimeLabel) -> int:
    """
    Baseline directional bias purely from regime classification.
    +1 = long bias, -1 = short bias, 0 = no signal (no_trade/range/compression/reversal_watch).
    This is intentionally conservative per project risk context: only clear trend
    regimes generate a directional bias in the baseline; everything else is NO TRADE.
    """
    if regime == RegimeLabel.TREND_UP:
        return 1
    elif regime == RegimeLabel.TREND_DOWN:
        return -1
    return 0


class EventDrivenBacktester:
    """
    Processes a DataFrame of candles (with 'regime' column already attached) one
    row at a time, opening/closing at most one position, applying spread/slippage
    from config, and using the SIGNAL GRID barrier logic (TP1 / stop from the
    signal_grid config, equal-step spec) for exits - so backtest outcomes mirror
    the live execution grid rather than the training-label barriers.
    """

    def __init__(self, cfg: dict, asset_key: str = None):
        self.cfg = cfg
        self.asset_key = asset_key
        asset_cfg = cfg.get("assets", {}).get(asset_key, {}) if asset_key else {}
        self.asset_cfg = asset_cfg
        bt_cfg = cfg["backtest"]
        lab_cfg = cfg["labeling"]
        # W6 (audit 2026-08-10): honour the per-asset spread/slippage overrides
        # (assets.<key>.spread_usd / slippage_usd) exactly like ensemble_backtest,
        # instead of always applying the global backtest defaults (0.25 spread /
        # 0.05 slippage) that made FX runs garbage (~460 pips of slippage).
        self.spread = asset_cfg.get("spread_usd", bt_cfg["spread_points"] / 100.0)
        self.slippage = asset_cfg.get("slippage_usd", bt_cfg["slippage_points"] / 100.0)
        self.balance = bt_cfg["initial_balance"]
        self.risk_pct = bt_cfg["risk_per_trade_pct"] / 100.0
        self.horizon_n = int(lab_cfg["horizon_candles_n"])
        # W5 (audit 2026-08-10): this engine used to report PnL in price units
        # (direction * price_delta) while ensemble_backtest reports account money
        # (price_pnl * volume * point_value_lot), making baseline vs ensemble
        # tables incomparable. Align it: money = price_pnl * volume * pvl.
        self.volume = float(bt_cfg.get("volume", 0.10))
        self.point_value_lot = float(
            asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0))
        )

        # Barrier distances come from the SIGNAL GRID (signal_grid:, per-asset
        # overrides allowed) so backtest exits mirror the live Telegram/MT5 grid
        # (TP1 = 1*step, stop = 3*step) instead of the training-label barriers.
        # Legacy labeling keys are kept as fallback for minimal/test configs.
        grid_cfg = get_signal_grid(cfg, asset_cfg)
        # Early breakeven trigger (fraction of the target distance). 1.0 = legacy.
        self.be_trigger_mult = float(grid_cfg.get("breakeven_trigger_atr", 1.0))
        self.progress_stop_enabled = bool(grid_cfg.get("progress_stop_enabled", False))
        self.progress_stop_ratio = float(grid_cfg.get("progress_stop_ratio", 0.5))
        self.progress_stop_atr = float(grid_cfg.get("progress_stop_atr", 0.3))
        method = lab_cfg.get("method", "fixed")
        if method == "atr_scaled":
            self.use_atr_scaled = True
            self.target_x_mult = float(
                grid_cfg.get("tp1_mult", lab_cfg.get("tp1_atr_multiplier", 1.0))
            )
            self.stop_y_mult = float(
                grid_cfg.get("stop_mult", lab_cfg.get("stop_atr_multiplier", 1.0))
            )
            self.atr_col = lab_cfg.get("atr_column", "atr")
            # Legacy fixed-barrier fallbacks for when the ATR column is absent.
            self.target_x = float(lab_cfg.get("target_pips_x", 0.0))
            self.stop_y = float(lab_cfg.get("stop_pips_y", 0.0))
        else:
            self.use_atr_scaled = False
            self.atr_col = "atr"
            self.target_x_mult = 0.0
            self.stop_y_mult = 0.0
            self.target_x = float(lab_cfg["target_pips_x"])
            self.stop_y = float(lab_cfg["stop_pips_y"])

        self.trades: List[Trade] = []
        self.equity_curve: List[float] = [self.balance]

    def run(self, df: pd.DataFrame) -> List[Trade]:
        """
        df must contain: timestamp_utc, open, high, low, close, session, regime.
        Iterates candle-by-candle. At most one open position at a time.
        Signal decided at close of candle i; entry executed at open of candle i+1.
        """
        n = len(df)
        open_position: Optional[Trade] = None
        pending_signal: Optional[int] = None  # signal decided at i, to be acted on at i+1
        entry_bar: Optional[int] = None  # index of the candle where the current position opened

        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        timestamps = df["timestamp_utc"].values
        sessions = df["session"].values
        regimes = df["regime"].values
        atrs = df[self.atr_col].values if self.atr_col in df.columns else None

        for i in range(n):
            # 1. Execute any pending signal from the PREVIOUS candle's close, using THIS candle's open
            if open_position is None and pending_signal is not None and pending_signal != 0:
                direction = pending_signal
                entry_price = opens[i] + (self.spread / 2 if direction == 1 else -self.spread / 2)

                # Barrier sizing: ATR-scaled (matches labeling.method=atr_scaled) or fixed pips.
                barrier_ok = True
                be_trigger_mult = self.be_trigger_mult
                if self.use_atr_scaled:
                    signal_i = i - 1 if i > 0 else i
                    atr_val = float(atrs[signal_i]) if atrs is not None else float("nan")
                    if pd.isna(atr_val) or atr_val <= 0:
                        atr_val = 0.0
                    if atr_val > 0:
                        regime_name = str(regimes[signal_i])
                        if hasattr(regimes[signal_i], "value"):
                            regime_name = regimes[signal_i].value
                        grid = get_signal_grid(self.cfg, self.asset_cfg, regime=regime_name)
                        step = resolve_signal_step(atr_val, grid)
                        stop_mult = float(grid.get("stop_mult", self.stop_y_mult))
                        target_mult = float(grid.get("tp1_mult", self.target_x_mult))
                        be_trigger_mult = float(grid.get("breakeven_trigger_atr", self.be_trigger_mult))
                        stop_price = entry_price - direction * step * stop_mult
                        target_price = entry_price + direction * step * target_mult
                    elif self.target_x > 0 and self.stop_y > 0:
                        stop_price = entry_price - direction * self.stop_y
                        target_price = entry_price + direction * self.target_x
                    else:
                        barrier_ok = False
                else:
                    stop_price = entry_price - direction * self.stop_y
                    target_price = entry_price + direction * self.target_x

                if not barrier_ok:
                    pending_signal = None  # cannot size targets/stops -> skip this entry
                else:
                    open_position = Trade(
                        entry_ts=int(timestamps[i]),
                        entry_price=entry_price,
                        direction=direction,
                        stop_price=stop_price,
                        target_price=target_price,
                        session=str(sessions[i]),
                        regime_at_entry=str(regimes[i - 1]) if i > 0 else str(regimes[i]),
                    )
                    open_position._be_trigger_mult = be_trigger_mult
                    entry_bar = i
                    pending_signal = None

            # 2. If a position is open, check for target/stop hit using the high/low
            # of the NEXT candle and beyond. Exits are never evaluated on the entry
            # candle itself (a signal known at candle i-1's close is acted on at i's
            # OPEN; using this same candle's high/low to close would be look-ahead).
            if open_position is not None and (entry_bar is None or i > entry_bar):
                direction = open_position.direction

                # Early breakeven (configurable): move the stop to entry once price reaches
                # be_trigger_mult * target-distance in our favor.
                if not getattr(open_position, "_be_triggered", False):
                    be_level = (open_position.entry_price
                                + direction * getattr(open_position, "_be_trigger_mult", self.be_trigger_mult)
                                * (open_position.target_price - open_position.entry_price))
                    if (direction == 1 and highs[i] >= be_level) or (direction == -1 and lows[i] <= be_level):
                        open_position.stop_price = open_position.entry_price
                        open_position._be_triggered = True

                hit_target = (highs[i] >= open_position.target_price) if direction == 1 else (lows[i] <= open_position.target_price)
                hit_stop = (lows[i] <= open_position.stop_price) if direction == 1 else (highs[i] >= open_position.stop_price)

                candles_held = self._candles_since(df, open_position.entry_ts, timestamps[i])

                hit_progress_stop = False
                if self.progress_stop_enabled:
                    progress_bars = int(self.horizon_n * self.progress_stop_ratio)
                    if candles_held >= progress_bars:
                        atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0
                        prog = (highs[i] - open_position.entry_price) if direction == 1 else (open_position.entry_price - lows[i])
                        prog_atr = prog / max(atr_val, 1e-6)
                        if prog_atr < self.progress_stop_atr:
                            hit_progress_stop = True

                exit_reason = None
                exit_price = None
                if hit_target and hit_stop:
                    # Conservative: ambiguous same-candle double touch -> assume stop hit first
                    exit_reason, exit_price = "stop", open_position.stop_price
                elif hit_stop:
                    exit_reason, exit_price = "stop", open_position.stop_price
                elif hit_target:
                    exit_reason, exit_price = "target", open_position.target_price
                elif hit_progress_stop:
                    exit_reason, exit_price = "progress_stop", closes[i]
                elif candles_held >= self.horizon_n:
                    exit_reason, exit_price = "timeout", closes[i]

                if exit_reason is not None:
                    exit_price += (-self.slippage if direction == 1 else self.slippage)
                    open_position.exit_ts = int(timestamps[i])
                    open_position.exit_price = exit_price
                    open_position.exit_reason = exit_reason
                    open_position.pnl = direction * (exit_price - open_position.entry_price) * self.volume * self.point_value_lot
                    self.balance += open_position.pnl
                    self.equity_curve.append(self.balance)
                    self.trades.append(open_position)
                    open_position = None
                    entry_bar = None

            # 3. Decide signal AT THE CLOSE of this candle, for execution at i+1 (never at i - that's look-ahead)
            if open_position is None:
                pending_signal = rule_based_signal(regimes[i])

        return self.trades

    @staticmethod
    def _candles_since(df: pd.DataFrame, entry_ts: int, current_ts: int) -> int:
        """Count candles elapsed between entry and current timestamp using the DataFrame's own spacing."""
        diffs = df["timestamp_utc"].diff().dropna()
        valid = diffs[diffs > 0]
        step = valid.mode().iloc[0] if len(valid) > 0 else 1
        diff = current_ts - entry_ts
        return int(diff / step) if (step > 0 and not np.isnan(step)) else 0
