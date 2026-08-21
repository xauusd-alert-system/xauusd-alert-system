"""
Shared MT5 broker context for the TradeGroup demo executor (ТЗ P1.5 §8/§9).

Responsibilities:

* ``snapshot()`` — read a FRESH symbol/account snapshot from the terminal.
  Nothing is taken from stale cached state; when the terminal is unavailable
  the snapshot raises ``BrokerUnavailable`` (fail-closed, no stale fallback).
* ``validate_geometry()`` — the strategic geometry from ``TradeGroupSpec`` is
  IMMUTABLE; the adapter only aligns to tick_size and verifies broker
  constraints (stops/freeze distance, volume grid, cost envelope). It NEVER
  stretches TP/SL; any violation raises ``GeometryRejected`` with
  ``ORDER_GEOMETRY_INVALID`` and the group must go to REJECTED.
* ``build_comment()`` — broker comment ``TG:<groupId>|L:<legId>|<asset>``,
  truncated to the broker comment limit; full ids always live in the ledger.
"""
from __future__ import annotations

from typing import Any

from execution.trade_geometry import (
    BrokerSnapshot,
    GeometryRejected,
    is_tick_aligned,
)
from execution.trade_group import TradeGroupSpec, new_leg_id

ORDER_GEOMETRY_INVALID = "ORDER_GEOMETRY_INVALID"
ACCOUNT_MODE_UNKNOWN = "ACCOUNT_MODE_UNKNOWN"


class BrokerUnavailable(RuntimeError):
    """MT5 terminal unavailable; never substitute stale values."""


class AccountModeUnknown(RuntimeError):
    """Account margin mode cannot be determined; execution is forbidden."""


def _align(price: float, tick_size: float) -> float:
    if tick_size <= 0.0:
        return float(price)
    return round(round(float(price) / tick_size) * tick_size, 10)


class MT5BrokerContext:
    """Fresh-read broker context around an ``import MetaTrader5 as mt5`` module."""

    def __init__(self, mt5, magic: int = 777111, comment_limit: int = 63):
        self.mt5 = mt5
        self.magic = int(magic)
        self.comment_limit = int(comment_limit)

    # ------------------------------------------------------------------
    # Fresh snapshots (ТЗ §8: no stale cache when MT5 is available)
    # ------------------------------------------------------------------

    def account_info(self) -> dict[str, Any]:
        acc = self.mt5.account_info()
        if acc is None:
            raise BrokerUnavailable("account_info() returned None")

        def _int_or(value, default: int) -> int:
            # 0 is a VALID mode (ACCOUNT_TRADE_MODE_DEMO / RETAIL_HEDGING), so
            # `or default` would wrongly map it to the fallback
            return default if value is None else int(value)

        return {
            "login": int(getattr(acc, "login", 0) or 0),
            "balance": float(getattr(acc, "balance", 0.0) or 0.0),
            "equity": float(getattr(acc, "equity", 0.0) or 0.0),
            "currency": str(getattr(acc, "currency", "USD") or "USD"),
            "trade_mode": _int_or(getattr(acc, "trade_mode", None), -1),
            "margin_mode": _int_or(getattr(acc, "margin_mode", None), -1),
        }

    def account_mode(self) -> str:
        """hedging | netting | unknown — never guessed (ТЗ §7)."""
        mt5 = self.mt5
        acc = self.account_info()
        margin_mode = acc["margin_mode"]
        hedging = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
        netting = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", None)
        if hedging is not None and margin_mode == hedging:
            return "hedging"
        if netting is not None and margin_mode == netting:
            return "netting"
        return "unknown"

    def symbol_snapshot(self, symbol: str) -> dict[str, Any]:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise BrokerUnavailable(f"symbol_info({symbol}) returned None")
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None or getattr(tick, "bid", 0.0) <= 0.0 or getattr(tick, "ask", 0.0) <= 0.0:
            raise BrokerUnavailable(f"no fresh tick for {symbol}")
        point = float(getattr(info, "point", 0.0) or 0.0)
        return {
            "symbol": symbol,
            "digits": int(getattr(info, "digits", 0) or 0),
            "point": point,
            "tick_size": float(getattr(info, "trade_tick_size", 0.0) or point or 0.0),
            "trade_stops_level": int(getattr(info, "trade_stops_level", 0) or 0),
            "trade_freeze_level": int(getattr(info, "trade_freeze_level", 0) or 0),
            "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
            "volume_max": float(getattr(info, "volume_max", 0.0) or 0.0),
            "volume_step": float(getattr(info, "volume_step", 0.0) or 0.0),
            "contract_size": float(getattr(info, "trade_contract_size", 0.0) or 0.0),
            "spread": abs(float(tick.ask) - float(tick.bid)),
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "execution_mode": str(getattr(info, "trade_exec_mode", "unknown")),
            "account_margin_mode": self.account_mode(),
        }

    def broker_snapshot(self, symbol: str) -> BrokerSnapshot:
        """The geometry engine's pure snapshot view (for validation reuse)."""
        raw = self.symbol_snapshot(symbol)
        account = self.account_info()
        return BrokerSnapshot(
            symbol_point=raw["point"],
            tick_size=raw["tick_size"],
            digits=raw["digits"],
            trade_stops_level=raw["trade_stops_level"],
            trade_freeze_level=raw["trade_freeze_level"],
            spread=raw["spread"],
            contract_size=raw["contract_size"],
            volume_min=raw["volume_min"],
            volume_max=raw["volume_max"],
            volume_step=raw["volume_step"],
            execution_mode=raw["execution_mode"],
            account_margin_mode=raw["account_margin_mode"],
            balance=account["balance"],
        )

    # ------------------------------------------------------------------
    # Normalization — align only, never stretch (ТЗ §9)
    # ------------------------------------------------------------------

    def normalize_geometry(self, spec: TradeGroupSpec, symbol: str) -> dict[str, float]:
        """Tick-align the immutable levels; raises GeometryRejected when the
        exact validated geometry cannot be executed."""
        snapshot = self.symbol_snapshot(symbol)
        tick = snapshot["tick_size"]
        if tick <= 0.0:
            raise GeometryRejected(ORDER_GEOMETRY_INVALID,
                                   f"non-positive tick_size {tick} for {symbol}")
        levels = {
            "tp1": _align(spec.geometry.tp1, tick),
            "tp2": _align(spec.geometry.tp2, tick),
            "tp3": _align(spec.geometry.tp3, tick),
            "sl": _align(spec.geometry.sl, tick),
        }
        for name, price in levels.items():
            if not is_tick_aligned(price, tick):
                raise GeometryRejected(
                    ORDER_GEOMETRY_INVALID,
                    f"{name}={price} not aligned to tick_size={tick}"
                )
        return levels

    def validate_geometry(self, spec: TradeGroupSpec, symbol: str) -> dict[str, float]:
        """Full broker-constraint validation of the approved geometry.

        Checks (ТЗ §9/§31/§32): tick alignment, broker minimum stop distance
        (stops/freeze + spread), TP1 net distance vs round-trip cost, and the
        volume grid for every leg. Never widens TP/SL.
        """
        levels = self.normalize_geometry(spec, symbol)
        snapshot = self.symbol_snapshot(symbol)
        min_stop = (
            max(snapshot["trade_stops_level"], snapshot["trade_freeze_level"])
            * snapshot["point"] + snapshot["spread"]
        )
        side = 1.0 if spec.side == "long" else -1.0
        sl_distance = abs(levels["sl"] - spec.entry.reference)
        if sl_distance < min_stop - 1e-9:
            raise GeometryRejected(
                ORDER_GEOMETRY_INVALID,
                f"SL distance {sl_distance:.6g} < broker minimum {min_stop:.6g}"
            )
        tp1_distance = abs(levels["tp1"] - spec.entry.reference)
        if tp1_distance <= snapshot["spread"] + 1e-9:
            raise GeometryRejected(
                ORDER_GEOMETRY_INVALID,
                f"TP1 distance {tp1_distance:.6g} <= spread {snapshot['spread']:.6g}"
            )
        for leg, volume in zip((1, 2, 3), [round(spec.risk.total_volume * t.allocation, 8)
                                           for t in spec.targets]):
            step = snapshot["volume_step"]
            minimum = snapshot["volume_min"]
            if volume <= 0.0 or volume < minimum - 1e-9:
                raise GeometryRejected(
                    ORDER_GEOMETRY_INVALID,
                    f"leg{leg} volume {volume} below volume_min {minimum}"
                )
            if step > 0.0 and abs(round(volume / step) * step - volume) > 1e-6:
                raise GeometryRejected(
                    ORDER_GEOMETRY_INVALID,
                    f"leg{leg} volume {volume} not a multiple of volume_step {step}"
                )
        return levels

    # ------------------------------------------------------------------
    # Comment / correlation (ТЗ §34)
    # ------------------------------------------------------------------

    def build_comment(self, group_id: str, leg: int | None = None) -> str:
        comment = f"TG:{group_id}"
        if leg is not None:
            comment += f"|L:{new_leg_id(group_id, leg)}"
        comment += f"|{self.magic}"
        if len(comment) > self.comment_limit:
            comment = comment[: self.comment_limit]
        return comment

    def parse_comment(self, comment: str) -> str | None:
        """Extract the group id from a broker comment ('TG:<groupId>|...')."""
        if not comment:
            return None
        for token in str(comment).split("|"):
            if token.startswith("TG:"):
                return token[3:]
        return None
