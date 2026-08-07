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
    # Audit fields (exit-path profiling): which barriers were reached before
    # the final exit, and the ORIGINAL stop distance (before any BE/trailing
    # move), so net R = pnl / money(|entry - initial_stop|) can be computed.
    tp1_hit: bool = False
    tp2_hit: bool = False
    initial_stop_price: Optional[float] = None
    # Per-trade exit policy resolved from signal_grid.regime_overrides at entry
    # (quant audit 2026-08-07, Claude plan action 4): trend -> wide/late,
    # range -> fast/early. Defaults equal the legacy global grid.
    be_trigger_mult: float = 1.0
    trailing_atr_mult: Optional[float] = None
    scaleout1: float = 0.5
    scaleout2: float = 0.3


def _regime_name(r):
    """Normalize a regime value (RegimeLabel enum or str) to its string key
    (e.g. 'trend_up'), so signal_grid.regime_overrides lookups match."""
    return r.value if hasattr(r, "value") else str(r)


class EnsembleBacktester:
    def __init__(self, cfg: dict, asset_key: str = "XAUUSD"):
        self.cfg = cfg
        self.asset_key = asset_key
        self.asset_cfg = cfg.get("assets", {}).get(asset_key, {})
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
        self.volume = bt_cfg.get("volume", 0.10)  # базовый объём для бэктеста
        self.commission_per_trade = bt_cfg.get("commission_per_trade", 0.07)
        self.swap_per_night = bt_cfg.get("swap_per_night", 0.0)
        # Validation of scaleout lot sizes (Task 7 / Section 5.2A)
        from execution.portfolio_allocator import validate_scaleout_tranches
        self.strict_scaleout_validation = bool(bt_cfg.get("strict_scaleout_validation", False))
        is_valid, err_msg, _ = validate_scaleout_tranches(self.volume, [0.5, 0.3, 0.2], raise_on_invalid=self.strict_scaleout_validation)
        if not is_valid and self.strict_scaleout_validation:
            raise ValueError(f"Scale-out validation error: {err_msg}")
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
        self.progress_stop_enabled = bool(grid_cfg.get("progress_stop_enabled", False))
        self.progress_stop_ratio = float(grid_cfg.get("progress_stop_ratio", 0.5))
        self.progress_stop_atr = float(grid_cfg.get("progress_stop_atr", 0.3))
        self.trailing_atr_mult = grid_cfg.get("trailing_atr_mult")  # None = legacy no-trail

        self.horizon_n = lab_cfg.get("horizon_candles_n", 36)
        self.trades: List[Trade] = []
        # Fill mode (quant audit, week-1 look-ahead check): "next_open" (honest
        # default) enters at the OPEN of the bar AFTER the signal bar;
        # "signal_close" enters at the CLOSE of the signal bar itself — this is
        # a look-ahead and MUST only be used for measurement (diag_entry_timing).
        self.fill_mode = str(bt_cfg.get("fill_mode", "next_open"))
        # Limit-entry mode (audit Q4b): fill at signal_close +/- limit_frac*step
        # with a `limit_timeout` bar window; unfilled limits are cancelled.
        self.limit_frac = float(bt_cfg.get("limit_frac", 0.25))
        self.limit_timeout = int(bt_cfg.get("limit_timeout", 2))
        # Signals that fired while a position was open (one-position-at-a-time
        # constraint) — the queue-loss measurement input.
        self.rejected_signals: List[dict] = []

    def simulate_blocked_entry(self, df: pd.DataFrame, signal_bar: int,
                               direction: int) -> Optional[Trade]:
        """Queue-loss measurement: what WOULD a rejected signal have earned.

        The signal fired at `signal_bar` while a position was open; the
        hypothetical honest fill is the OPEN of `signal_bar + 1`. Runs the
        engine on the sliced frame with a forced entry (max_trades=1) so the
        exit logic is bit-identical to real trades. Returns the trade or None
        (no exit within the slice).
        """
        if signal_bar + 1 >= len(df):
            return None
        sub = df.iloc[signal_bar + 1:].reset_index(drop=True)
        sim = EnsembleBacktester(self.cfg, asset_key=self.asset_key)
        sim.fill_mode = "next_open"
        trades = sim.run(sub, forced_direction=direction, max_trades=1)
        return trades[0] if trades else None

    def _apply_slippage(self, price: float, direction: int) -> float:
        return price + (self.slippage * direction)

    def _money(self, price_pnl: float) -> float:
        """Convert a price-space PnL to account money using lot size and contract multiplier."""
        return price_pnl * self.volume * self.point_value_lot

    def run(self, df: pd.DataFrame, forced_direction: int = None,
            max_trades: int = None) -> List[Trade]:
        """Run the backtest over `df`.

        forced_direction: if set (1/-1), a trade is opened at the first bar
            without evaluating signals (used by simulate_blocked_entry for the
            queue-loss measurement — the hypothetical entry of a signal that
            was rejected because a position was already open).
        max_trades: stop after this many closed trades (also used by the
            queue-loss simulation).
        """
        n = len(df)
        open_position: Optional[Trade] = None
        self.rejected_signals = []
        # HIGH 7 (no-look-ahead): a position opened at candle i open may only be
        # exited from candle i+1 onwards - same-candle entries never evaluate TP/SL.
        entry_bar: Optional[int] = None
        pending_direction: Optional[int] = None
        # Limit-entry state: (limit_price, direction, bars_waited)
        pending_limit: Optional[tuple] = None

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
            if open_position is None and forced_direction is not None and i == 0:
                # Queue-loss simulation: forced hypothetical entry at the first
                # bar of the sliced frame (open of the bar AFTER the signal).
                pending_direction = int(forced_direction)
                forced_direction = None

            # LIMIT-ENTRY MODE (audit Q4b): try to fill the pending limit on
            # this bar's intrabar range before falling back to market logic.
            if (open_position is None and pending_limit is not None
                    and self.fill_mode == "limit"):
                limit_price, ldir, bars_waited = pending_limit
                touched = (lows[i] <= limit_price) if ldir == 1 else (highs[i] >= limit_price)
                if touched:
                    direction = ldir
                    entry_price = limit_price
                    atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0
                    reg_name = _regime_name(regimes[i - 1]) if i > 0 else _regime_name(regimes[i])
                    grid = get_signal_grid(self.cfg, self.asset_cfg, regime=reg_name)
                    tp1_mult = float(grid.get("tp1_mult", self.tp1_mult))
                    tp2_mult = float(grid.get("tp2_mult", self.tp2_mult))
                    tp3_mult = float(grid.get("tp3_mult", self.tp3_mult))
                    stop_mult = float(grid.get("stop_mult", self.stop_mult))
                    be_trigger = float(grid.get("breakeven_trigger_atr", self.be_trigger_mult))
                    trail_mult = grid.get("trailing_atr_mult")
                    scaleout = grid.get("scaleout") or {}
                    so1 = float(scaleout.get("tp1_ratio", 0.5)) if isinstance(scaleout, dict) else 0.5
                    so2 = float(scaleout.get("tp2_ratio", 0.3)) if isinstance(scaleout, dict) else 0.3
                    open_position = Trade(
                        entry_ts=int(timestamps[i]),
                        entry_price=entry_price,
                        direction=direction,
                        stop_price=entry_price - direction * (atr_val * stop_mult),
                        tp1_price=entry_price + direction * (atr_val * tp1_mult),
                        tp2_price=entry_price + direction * (atr_val * tp2_mult),
                        tp3_price=entry_price + direction * (atr_val * tp3_mult),
                        initial_stop_price=entry_price - direction * (atr_val * stop_mult),
                        session=str(sessions[i]),
                        regime_at_entry=reg_name,
                        volume=self.volume,
                        commission=self.commission_per_trade,
                        swap=0.0,
                        be_trigger_mult=be_trigger,
                        trailing_atr_mult=trail_mult,
                        scaleout1=so1,
                        scaleout2=so2,
                    )
                    entry_bar = i
                    tp1_hit = False
                    tp2_hit = False
                    be_triggered = False
                    remaining_ratio = 1.0
                    accumulated_pnl = 0.0
                    pending_limit = None
                    pending_direction = None
                else:
                    bars_waited += 1
                    if bars_waited >= self.limit_timeout:
                        pending_limit = None  # unfilled -> cancelled
                    else:
                        pending_limit = (limit_price, ldir, bars_waited)

            if open_position is None and pending_direction is not None and pending_direction != 0:
                direction = pending_direction
                entry_price = opens[i] + (self.spread / 2 if direction == 1 else -self.spread / 2)
                entry_price = self._apply_slippage(entry_price, direction)

                atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0

                # Per-trade exit policy resolved from the regime at SIGNAL time
                # (bar i-1, same as regime_at_entry). signal_grid.regime_overrides
                # lets trend regimes run wide and range regimes manage fast.
                reg_name = _regime_name(regimes[i - 1]) if i > 0 else _regime_name(regimes[i])
                grid = get_signal_grid(self.cfg, self.asset_cfg, regime=reg_name)
                tp1_mult = float(grid.get("tp1_mult", self.tp1_mult))
                tp2_mult = float(grid.get("tp2_mult", self.tp2_mult))
                tp3_mult = float(grid.get("tp3_mult", self.tp3_mult))
                stop_mult = float(grid.get("stop_mult", self.stop_mult))
                be_trigger = float(grid.get("breakeven_trigger_atr", self.be_trigger_mult))
                trail_mult = grid.get("trailing_atr_mult")
                scaleout = grid.get("scaleout") or {}
                so1 = float(scaleout.get("tp1_ratio", 0.5)) if isinstance(scaleout, dict) else 0.5
                so2 = float(scaleout.get("tp2_ratio", 0.3)) if isinstance(scaleout, dict) else 0.3

                stop_price = entry_price - direction * (atr_val * stop_mult)
                tp1_price = entry_price + direction * (atr_val * tp1_mult)
                tp2_price = entry_price + direction * (atr_val * tp2_mult)
                tp3_price = entry_price + direction * (atr_val * tp3_mult)

                open_position = Trade(
                    entry_ts=int(timestamps[i]),
                    entry_price=entry_price,
                    direction=direction,
                    stop_price=stop_price,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    tp3_price=tp3_price,
                    initial_stop_price=stop_price,
                    session=str(sessions[i]),
                    regime_at_entry=reg_name,
                    volume=self.volume,
                    commission=self.commission_per_trade,
                    swap=0.0,
                    be_trigger_mult=be_trigger,
                    trailing_atr_mult=trail_mult,
                    scaleout1=so1,
                    scaleout2=so2,
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
                                + direction * open_position.be_trigger_mult
                                * (open_position.tp1_price - open_position.entry_price))
                    if (direction == 1 and highs[i] >= be_level) or (direction == -1 and lows[i] <= be_level):
                        open_position.stop_price = open_position.entry_price
                        be_triggered = True

                # 1. TP1 -> scaleout1 закрываем, Стоп в БЕЗУБЫТОК
                if not tp1_hit and hit_tp1:
                    tp1_hit = True
                    # HIGH 7: convert price-space PnL to money via volume * point_value_lot.
                    pnl_tp1 = self._money(open_position.scaleout1 * direction * (open_position.tp1_price - open_position.entry_price))
                    accumulated_pnl += pnl_tp1
                    remaining_ratio = 1.0 - open_position.scaleout1
                    open_position.stop_price = open_position.entry_price  # BREAKEVEN

                # 2. TP2 -> scaleout2 закрываем
                if tp1_hit and not tp2_hit and hit_tp2:
                    tp2_hit = True
                    pnl_tp2 = self._money(open_position.scaleout2 * direction * (open_position.tp2_price - open_position.entry_price))
                    accumulated_pnl += pnl_tp2
                    remaining_ratio = 1.0 - open_position.scaleout1 - open_position.scaleout2

                # 2b. TRAILING (v4b "trailing-runner") — only after TP1+TP2 and if trailing_atr_mult is set
                trailing_exit = False
                if tp1_hit and tp2_hit and open_position.trailing_atr_mult is not None and remaining_ratio > 0:
                    atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0
                    if direction == 1:
                        trail_stop = highs[i] - float(open_position.trailing_atr_mult) * atr_val
                        if open_position.stop_price < trail_stop:
                            open_position.stop_price = trail_stop
                        if lows[i] <= open_position.stop_price:
                            trailing_exit = True
                    else:
                        trail_stop = lows[i] + float(open_position.trailing_atr_mult) * atr_val
                        if open_position.stop_price > trail_stop:
                            open_position.stop_price = trail_stop
                        if highs[i] >= open_position.stop_price:
                            trailing_exit = True

                # 3. Финальный выход (TP3, Стоп, Progress-Stop или Таймаут)
                exit_reason = None
                exit_price = None

                hit_progress_stop = False
                if self.progress_stop_enabled and not tp1_hit:
                    progress_bars = int(self.horizon_n * self.progress_stop_ratio)
                    if candles_held >= progress_bars:
                        atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0
                        prog = (highs[i] - open_position.entry_price) if direction == 1 else (open_position.entry_price - lows[i])
                        prog_atr = prog / max(atr_val, 1e-6)
                        if prog_atr < self.progress_stop_atr:
                            hit_progress_stop = True

                if hit_tp3:
                    exit_reason = "tp3_runner"
                    pnl_tp3 = self._money(remaining_ratio * direction * (open_position.tp3_price - open_position.entry_price))
                    accumulated_pnl += pnl_tp3
                    exit_price = self._apply_slippage(open_position.tp3_price, -direction)
                elif trailing_exit:
                    exit_reason = "trailing"
                    pnl_trail = self._money(remaining_ratio * direction * (open_position.stop_price - open_position.entry_price))
                    accumulated_pnl += pnl_trail
                    exit_price = self._apply_slippage(open_position.stop_price, -direction)
                elif hit_stop:
                    exit_reason = "breakeven" if (tp1_hit or be_triggered) else "stop"
                    pnl_stop = self._money(remaining_ratio * direction * (open_position.stop_price - open_position.entry_price))
                    accumulated_pnl += pnl_stop
                    exit_price = self._apply_slippage(open_position.stop_price, -direction)
                elif hit_progress_stop:
                    exit_reason = "progress_stop"
                    pnl_prog = self._money(remaining_ratio * direction * (closes[i] - open_position.entry_price))
                    accumulated_pnl += pnl_prog
                    exit_price = self._apply_slippage(closes[i], -direction)
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
                    open_position.tp1_hit = tp1_hit
                    open_position.tp2_hit = tp2_hit
                    self.balance += accumulated_pnl
                    self.trades.append(open_position)
                    open_position = None
                    entry_bar = None
                    if max_trades is not None and len(self.trades) >= max_trades:
                        return self.trades

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

                # LIMIT-ENTRY MODE (audit Q4b): convert the pending market
                # signal into a limit at signal_close +/- limit_frac * step;
                # fills are attempted intrabar on the next bars (timeout).
                if (self.fill_mode == "limit" and pending_direction != 0):
                    ldir = pending_direction
                    atr_here = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0
                    limit_price = closes[i] + (self.limit_frac * atr_here * ldir)
                    pending_limit = (float(limit_price), ldir, 0)
                    pending_direction = None

                # LOOK-AHEAD MEASUREMENT MODE ONLY: fill at the close of the
                # SIGNAL bar itself instead of the next bar's open. Any edge
                # that collapses under this comparison is partly look-ahead.
                if pending_direction != 0 and self.fill_mode == "signal_close":
                    direction = pending_direction
                    entry_price = closes[i] + (self.spread / 2 if direction == 1 else -self.spread / 2)
                    entry_price = self._apply_slippage(entry_price, direction)
                    atr_val = atrs[i] if (atrs is not None and not np.isnan(atrs[i])) else 1.0
                    reg_name = _regime_name(regimes[i])
                    grid = get_signal_grid(self.cfg, self.asset_cfg, regime=reg_name)
                    tp1_mult = float(grid.get("tp1_mult", self.tp1_mult))
                    tp2_mult = float(grid.get("tp2_mult", self.tp2_mult))
                    tp3_mult = float(grid.get("tp3_mult", self.tp3_mult))
                    stop_mult = float(grid.get("stop_mult", self.stop_mult))
                    be_trigger = float(grid.get("breakeven_trigger_atr", self.be_trigger_mult))
                    trail_mult = grid.get("trailing_atr_mult")
                    scaleout = grid.get("scaleout") or {}
                    so1 = float(scaleout.get("tp1_ratio", 0.5)) if isinstance(scaleout, dict) else 0.5
                    so2 = float(scaleout.get("tp2_ratio", 0.3)) if isinstance(scaleout, dict) else 0.3
                    open_position = Trade(
                        entry_ts=int(timestamps[i]),
                        entry_price=entry_price,
                        direction=direction,
                        stop_price=entry_price - direction * (atr_val * stop_mult),
                        tp1_price=entry_price + direction * (atr_val * tp1_mult),
                        tp2_price=entry_price + direction * (atr_val * tp2_mult),
                        tp3_price=entry_price + direction * (atr_val * tp3_mult),
                        initial_stop_price=entry_price - direction * (atr_val * stop_mult),
                        session=str(sessions[i]),
                        regime_at_entry=reg_name,
                        volume=self.volume,
                        commission=self.commission_per_trade,
                        swap=0.0,
                        be_trigger_mult=be_trigger,
                        trailing_atr_mult=trail_mult,
                        scaleout1=so1,
                        scaleout2=so2,
                    )
                    entry_bar = i
                    tp1_hit = False
                    tp2_hit = False
                    be_triggered = False
                    remaining_ratio = 1.0
                    accumulated_pnl = 0.0
                    pending_direction = None
            else:
                # Position already open: record any signal that WOULD have fired
                # (one-position-at-a-time constraint) for the queue-loss
                # measurement. Same gates as above.
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
                if sig.bias in ("long", "short"):
                    self.rejected_signals.append({
                        "bar": i,
                        "direction": 1 if sig.bias == "long" else -1,
                        "regime": str(reg_val.value if hasattr(reg_val, "value") else reg_val),
                        "session": str(sessions[i]),
                        "p_long": float(p_longs[i]),
                        "p_short": float(p_shorts[i]),
                    })

        return self.trades
