"""
OHLCV aggregator for the virtual market simulation.

Rolls simulated tick trades into OHLCV bars for a configurable bar
interval (in ticks) and returns them as pandas DataFrames.

CRITICAL: All returned DataFrames use the column name "timestamp" (Unix
seconds as int), NOT "timestamp_utc". This matches the schema expected by
data/mt5_provider.py:_normalize_rates(), which reads df["time"] from the
numpy structured array produced by the MT5 shim and renames it to
"timestamp". Keeping "timestamp" here guarantees compatibility.
"""

from __future__ import annotations

from typing import TypedDict

import pandas as pd

from simulation.engine.order import Trade


class Bar(TypedDict):
    """OHLCV bar internal representation."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class OHLCVAggregator:
    """Accumulates Trade flow and produces OHLCV bars per tick interval."""

    def __init__(
        self,
        bar_interval_ticks: int = 12,
        tick_duration_seconds: int = 5,
        start_tick: int = 0,
        start_timestamp: int = 0,  # <-- NEW: accepted but used only for
        # absolute wall-clock offset if provided
    ) -> None:
        self.bar_interval_ticks = bar_interval_ticks
        self.tick_duration_seconds = tick_duration_seconds
        self.start_tick = start_tick
        # Wall-clock anchor: if caller passes start_timestamp (Unix seconds)
        # we anchor bar times to that instead of tick-relative 0.
        self._wall_anchor: int = int(start_timestamp) if start_timestamp else 0
        self.last_price: float | None = None
        self._bars: dict[int, Bar] = {}  # bar_index -> Bar
        self._current_bar_tick: int | None = None
        self._current_bar: Bar | None = None

    # ------------------------------------------------------------------
    # Tick ingestion
    # ------------------------------------------------------------------
    def on_tick(self, trade: Trade) -> None:
        """Record a trade into the currently forming bar."""
        self.last_price = trade.price

        bar_index = trade.tick // self.bar_interval_ticks
        if self._current_bar_tick != bar_index:
            self._current_bar_tick = bar_index
            self._current_bar = Bar(
                time=self._bar_time(bar_index),
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                volume=trade.volume,
            )
            self._bars[bar_index] = self._current_bar
        else:
            bar = self._current_bar
            assert bar is not None
            bar["high"] = max(bar["high"], trade.price)
            bar["low"] = min(bar["low"], trade.price)
            bar["close"] = trade.price
            bar["volume"] += trade.volume

    def _bar_time(self, bar_index: int) -> int:
        """Unix seconds (int) for the opening time of a bar index."""
        if self._wall_anchor:
            # Absolute mode: anchor to the wall-clock start_timestamp.
            offset_ticks = self.start_tick // self.bar_interval_ticks + bar_index
            return self._wall_anchor + offset_ticks * self.bar_interval_ticks * self.tick_duration_seconds
        # Relative mode (legacy): pure tick arithmetic.
        return (
            (self.start_tick // self.bar_interval_ticks + bar_index)
            * self.bar_interval_ticks
            * self.tick_duration_seconds
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def get_bars(self, n: int = 100) -> pd.DataFrame:
        """Return the last `n` bars (allows the forming bar too)."""
        return self.get_bars_by_interval(self.bar_interval_ticks, n=n)

    def get_bars_by_interval(self, bar_interval: int, n: int = 100) -> pd.DataFrame:
        """
        Return the last `n` bars for an arbitrary tick interval.

        Returns columns: timestamp(int unix seconds), open, high, low,
        close, volume.
        """
        columns = ["timestamp", "open", "high", "low", "close", "volume"]
        if bar_interval <= 0:
            return pd.DataFrame(columns=columns)

        if bar_interval == self.bar_interval_ticks:
            bars: list[tuple[int, Bar]] = sorted(self._bars.items(), key=lambda kv: kv[0])
        else:
            bars = self._rebuild_bars(bar_interval)

        if not bars:
            return pd.DataFrame(columns=columns)

        bars = bars[-n:] if n > 0 else bars
        rows: list[dict[str, object]] = [
            {
                "timestamp": int(b["time"]),
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            }
            for _, b in bars
        ]
        df = pd.DataFrame(rows, columns=columns)
        return df.astype({"timestamp": "int64"})

    def _rebuild_bars(self, bar_interval: int) -> list[tuple[int, Bar]]:
        """Resample the base bars into a coarser tick interval."""
        if not self._bars or bar_interval < self.bar_interval_ticks:
            return []
        ratio = bar_interval / self.bar_interval_ticks
        merged: dict[int, Bar] = {}
        for bar_index, b in sorted(self._bars.items(), key=lambda kv: kv[0]):
            target_index = int(bar_index // ratio)
            if target_index not in merged:
                nb = Bar(**b)
                nb["time"] = self._resampled_bar_time(target_index, bar_interval)
                merged[target_index] = nb
            else:
                e = merged[target_index]
                e["high"] = max(e["high"], b["high"])
                e["low"] = min(e["low"], b["low"])
                e["close"] = b["close"]
                e["volume"] += b["volume"]
                e["open"] = merged[target_index]["open"]

        return sorted(merged.items(), key=lambda kv: kv[0])

    def _resampled_bar_time(self, target_index: int, bar_interval: int) -> int:
        """Unix seconds (int) for the open time of a resampled bar."""
        if self._wall_anchor:
            return self._wall_anchor + target_index * bar_interval * self.tick_duration_seconds
        base_time = (self.start_tick // self.bar_interval_ticks) * self.bar_interval_ticks * self.tick_duration_seconds
        return base_time + target_index * bar_interval * self.tick_duration_seconds

    def reset(self, new_start_tick: int = 0) -> None:
        """Clear all accumulated bars.

        ``new_start_tick`` should be set to the simulator's current tick
        so post-reset bar indices are computed relative to the new baseline
        instead of tick-0, giving correct wall-clock timestamps.
        """
        self._bars.clear()
        self._current_bar = None
        self._current_bar_tick = None
        self.last_price = None
        if new_start_tick:
            self.start_tick = new_start_tick
