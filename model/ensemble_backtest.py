"""
Ensemble-aware backtest wrapper with Multi-TP (Scale-Out) & Auto-Breakeven support.
Reads asset-specific spreads and simulates commissions/swaps/slippage.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, List
from regime.classifier import RegimeLabel
from model.ensemble import compute_ensemble_signal
from config.loader import get_signal_grid


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    direction: int  # +1 long, -1 short
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    session: str
    regime_at_entry: str
    volume: float
    commission: float
    swap: float
    exit_ts: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None


class EnsembleBacktester:
    def __init__(self, cfg: dict, asset_key: str = "XAUUSD"):
        self.cfg = cfg
        self.asset_key = asset_key
        bt_cfg = cfg.get("backtest", {})
        lab_cfg = cfg.get("labeling", {})
        asset_cfg = cfg.get("assets", {}).get(asset_key, {})

        # Читаем индивидуальный спред для актива (если прописан в assets), иначе общий дефолт
        self.spread = asset_cfg.get("spread_usd", bt_cfg.get("spread_points", 25) / 100.0)
        # HIGH BUG (FX unit mismatch): the global slippage_points default (5 -> 0.05
        # absolute price units) is a ~460-pip slippage on EURUSD (~1.08) and a ~177x-ATR
        # entry shift, instantly stopping every low-priced FX trade (0% win rate, pnl ==
        # -commission exactly). Mirror the spread_usd pattern with a per-asset
        # slippage_usd override (MT5's real slippage is the instrument-specific
        # deviation in points, never a global absolute). Gold keeps the old 0.05 default
        # so existing benchmark behaviour for metals/BTC is preserved unless overridden.
        self.slippage = asset_cfg.get(
            "slippage_usd", bt_cfg.get("slippage_points", 5) / 100.0
        )
        self.balance = bt_cfg.get("initial_balance", 100.0)
        self.volume = bt_cfg.get("volume", 0.01)  # базовый объём для бэктеста
        self.commission_per_trade = bt_cfg.get("commission_per_trade", 0.07)
        self.swap_per_night = bt_cfg.get("swap_per_night", 0.0)
        # HIGH 7: point_value_lot = USD notional per 1.0 lot per 1.0 price unit.
        # Converts price-space PnL into account money so it is comparable with
        # commission/swap (which are already money). Default 100 (e.g. XAUUSD
        # 1 lot = 100 oz -> $1 price move = $100 per lot). Per-asset override in
        # config: assets.<key>.point_value_lot.
        self.point_value_lot = asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0))

        # Signal grid (equal-step spec) with legacy labeling-key fallback.
        grid_cfg = get_signal_grid(cfg, asset_cfg)
        self.atr_col = lab_cfg.get("atr_column", "atr")
        self.tp1_mult = grid_cfg.get("tp1_mult", 1.0)
        self.tp2_mult = grid_cfg.get("tp2_mult", 2.0)
        self.tp3_mult = grid_cfg.get("tp3_mult", 3.0)
        self.stop_mult = grid_cfg.get("stop_mult", 3.0)
        # Early breakeven trigger (fraction of the TP1 distance). 1.0 = legacy
        # (BE only when TP1 hits); < 1.0 moves the stop to entry earlier, which
        # cuts the 3x-step loss tail for mean-reverting assets (FX).
        self.be_trigger_mult = float(grid_cfg.get("breakeven_trigger_atr", 1.0))

        self.horizon_n = lab_cfg.get("horizon_candles_n", 36)
        self.trades: List[Trade] = []

    def _apply_slippage(self, price: float, direction: int) -> float:
        return price + (self.slippage * direction)

    def _money(self, price_pnl: float) -> float:
        """Convert a price-space PnL to account money using lot size and contract multiplier."""
        return price_pnl * self.volume * self.point_value_lot

    def run(self, df: pd.DataFrame) -> List[Trade]:
        n = len(df)
        open_position: Optional[Trade] = None
        # HIGH 7 (no-look-ahead): a position opened at candle i open may only be
        # exited from candle i+1 onwards - same-candle entries never evaluate TP/SL.
        entry_bar: Optional[int] = None
        pending_direction: Optional[int] = None

        tp1_hit = False
        tp2_hit = False
        be_triggered = False
        remaining_ratio = 1.0

        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        timestamps = df["timestamp_utc"].values
        sessions = df["session"].values
        regimes = df["regime"].values

        atrs = df[self.atr_col].values if self.atr_col in df.columns else None
        p_longs = df.get("ml_p_long", pd.Series(0.5, index=df.index)).values
        p_shorts = df.get("ml_p_short", pd.Series(0.5, index=df.index)).values

        accumulated_pnl = 0.0

        for i in range(n):
            if open_position is None and pending_direction is not None and pending_direction != 0:
                direction = pending_direction
                entry_price = opens[i] + (self.spread / 2 if direction == 1 else -self.spread / 2)
                entry_price = self._apply_slippage(entry_price, direction)

                atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0

                stop_price = entry_price - direction * (atr_val * self.stop_mult)
                tp1_price = entry_price + direction * (atr_val * self.tp1_mult)
                tp2_price = entry_price + direction * (atr_val * self.tp2_mult)
                tp3_price = entry_price + direction * (atr_val * self.tp3_mult)

                open_position = Trade(
                    entry_ts=int(timestamps[i]),
                    entry_price=entry_price,
                    direction=direction,
                    stop_price=stop_price,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    tp3_price=tp3_price,
                    session=str(sessions[i]),
                    regime_at_entry=str(regimes[i - 1]) if i > 0 else str(regimes[i]),
                    volume=self.volume,
                    commission=self.commission_per_trade,
                    swap=0.0,
                )
                pending_direction = None
                entry_bar = i
                tp1_hit = False
                tp2_hit = False
                be_triggered = False
                remaining_ratio = 1.0
                accumulated_pnl = 0.0

            if open_position is not None and (entry_bar is None or i > entry_bar):
                direction = open_position.direction

                hit_tp1 = (highs[i] >= open_position.tp1_price) if direction == 1 else (lows[i] <= open_position.tp1_price)
                hit_tp2 = (highs[i] >= open_position.tp2_price) if direction == 1 else (lows[i] <= open_position.tp2_price)
                hit_tp3 = (highs[i] >= open_position.tp3_price) if direction == 1 else (lows[i] <= open_position.tp3_price)
                hit_stop = (lows[i] <= open_position.stop_price) if direction == 1 else (highs[i] >= open_position.stop_price)

                step = df["timestamp_utc"].diff().mode().iloc[0] if len(df) > 1 else 1
                try:
                    # df["timestamp_utc"] may be epoch-seconds ints (real backtests) or
                    # uniform Timedeltas (tests); NaT/NaN must not reach int().
                    step_secs = step.total_seconds() if hasattr(step, "total_seconds") else float(step)
                except (TypeError, ValueError):
                    step_secs = 1.0
                if step_secs != step_secs or step_secs <= 0:  # NaN or non-positive
                    step_secs = 1.0
                candles_held = int((timestamps[i] - open_position.entry_ts) / step_secs)

                # 0. EARLY BREAKEVEN (configurable): move the stop to entry as soon as
                # price reaches be_trigger_mult * (TP1 distance) in our favor — BEFORE TP1.
                if not tp1_hit and not be_triggered:
                    be_level = (open_position.entry_price
                                + direction * self.be_trigger_mult
                                * (open_position.tp1_price - open_position.entry_price))
                    if (direction == 1 and highs[i] >= be_level) or (direction == -1 and lows[i] <= be_level):
                        open_position.stop_price = open_position.entry_price
                        be_triggered = True

                # 1. TP1 -> 50% закрываем, Стоп в БЕЗУБЫТОК
                if not tp1_hit and hit_tp1:
                    tp1_hit = True
                    # HIGH 7: convert price-space PnL to money via volume * point_value_lot.
                    pnl_tp1 = self._money(0.5 * direction * (open_position.tp1_price - open_position.entry_price))
                    accumulated_pnl += pnl_tp1
                    remaining_ratio = 0.5
                    open_position.stop_price = open_position.entry_price  # BREAKEVEN

                # 2. TP2 -> 30% закрываем
                if tp1_hit and not tp2_hit and hit_tp2:
                    tp2_hit = True
                    pnl_tp2 = self._money(0.3 * direction * (open_position.tp2_price - open_position.entry_price))
                    accumulated_pnl += pnl_tp2
                    remaining_ratio = 0.2

                # 3. Финальный выход (TP3, Стоп или Таймаут)
                exit_reason = None
                exit_price = None

                if hit_tp3:
                    exit_reason = "tp3_runner"
                    pnl_tp3 = self._money(remaining_ratio * direction * (open_position.tp3_price - open_position.entry_price))
                    accumulated_pnl += pnl_tp3
                    exit_price = self._apply_slippage(open_position.tp3_price, -direction)
                elif hit_stop:
                    exit_reason = "breakeven" if (tp1_hit or be_triggered) else "stop"
                    pnl_stop = self._money(remaining_ratio * direction * (open_position.stop_price - open_position.entry_price))
                    accumulated_pnl += pnl_stop
                    exit_price = self._apply_slippage(open_position.stop_price, -direction)
                elif candles_held >= self.horizon_n:
                    exit_reason = "timeout"
                    pnl_time = self._money(remaining_ratio * direction * (closes[i] - open_position.entry_price))
                    accumulated_pnl += pnl_time
                    exit_price = self._apply_slippage(closes[i], -direction)

                if exit_reason is not None:
                    # Учитываем комиссию и своп (уже в денежных единицах)
                    days_held = max(1, (int(timestamps[i]) - open_position.entry_ts) // 86400)
                    swap = self.swap_per_night * days_held
                    accumulated_pnl -= open_position.commission + swap

                    open_position.exit_ts = int(timestamps[i])
                    open_position.exit_price = exit_price
                    open_position.exit_reason = exit_reason
                    open_position.swap = swap
                    open_position.pnl = accumulated_pnl
                    self.balance += accumulated_pnl
                    self.trades.append(open_position)
                    open_position = None
                    entry_bar = None

            if open_position is None:
                reg_val = regimes[i]
                if not isinstance(reg_val, RegimeLabel):
                    try:
                        reg_val = RegimeLabel(reg_val)
                    except ValueError:
                        reg_val = RegimeLabel.NO_TRADE

                sig = compute_ensemble_signal(
                    reg_val,
                    float(p_longs[i]),
                    float(p_shorts[i]),
                    self.cfg,
                    session=str(sessions[i]),
                    timestamp_utc=int(timestamps[i]),
                    asset_key=self.asset_key if hasattr(self, 'asset_key') else "XAUUSD"
                )
                pending_direction = {"long": 1, "short": -1, "no_trade": 0}[sig.bias]

        return self.trades