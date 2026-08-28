"""
MT5 Hedging adapter for TradeGroupSpec v1 (ТЗ P1.5 §11/§12).

Hedging accounts get THREE physical positions, one per leg:

    leg 1: volume=leg1, SL=spec.sl,     TP=spec.tp1
    leg 2: volume=leg2, SL=spec.sl,     TP=spec.tp2
    leg 3: volume=leg3, SL=spec.sl,     TP=spec.tp3

All legs share groupId/intentId/magic and carry the broker comment
``TG:<groupId>|L:<legId>|<magic>`` (truncated to the broker limit; full ids live
in the ledger). TP/SL are taken from the immutable spec — the adapter never
recomputes them (ТЗ §3).

The driver is duck-typed against the real MetaTrader5 Python package surface
(``import MetaTrader5 as mt5``), so it works unchanged on a Windows demo
terminal and in tests against a deterministic double.
"""

from __future__ import annotations

from typing import Any

from execution.mt5_common import MT5BrokerContext
from execution.trade_group import TradeGroupSpec, new_leg_id


class MT5HedgingDriver:
    """Three-physical-leg driver implementing the GroupDriver surface."""

    mode = "demo"
    account_mode = "hedging"

    def __init__(self, mt5, magic: int = 777111, comment_limit: int = 63):
        self.ctx = MT5BrokerContext(mt5, magic=magic, comment_limit=comment_limit)
        self.mt5 = mt5
        self.magic = int(magic)

    # ------------------------------------------------------------------
    # GroupDriver surface
    # ------------------------------------------------------------------

    def submit_leg(self, spec: TradeGroupSpec, leg: int, volume: float) -> dict[str, Any]:
        """Open one physical position for ``leg`` with the spec's SL/TP."""
        snapshot = self.ctx.symbol_snapshot(spec.broker_symbol)
        tick = snapshot["tick_size"]
        side = 1.0 if spec.side == "long" else -1.0
        price = snapshot["ask"] if spec.side == "long" else snapshot["bid"]
        order_type = self.mt5.ORDER_TYPE_BUY if spec.side == "long" else self.mt5.ORDER_TYPE_SELL
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": spec.broker_symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "deviation": 20,
            "magic": self.magic,
            "comment": self.ctx.build_comment(spec.group_id, leg),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
            "sl": float(spec.geometry.sl),
            "tp": float(spec.leg_price(leg)),
        }
        result = self.mt5.order_send(request)
        retcode = int(getattr(result, "retcode", -1) or -1)
        done = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        if retcode != done:
            return {
                "status": "rejected",
                "retcode": retcode,
                "comment": str(getattr(result, "comment", "") or ""),
                "order_id": getattr(result, "order", None),
                "requested_volume": float(volume),
                "filled_volume": 0.0,
                "fill_price": None,
            }
        position = self._resolve_position(spec, leg)
        if position is None:
            return {
                "status": "rejected",
                "retcode": retcode,
                "comment": "order accepted but position could not be resolved",
                "order_id": getattr(result, "order", None),
                "requested_volume": float(volume),
                "filled_volume": 0.0,
                "fill_price": None,
            }
        filled = float(getattr(result, "volume", 0.0) or 0.0) or float(position["volume"])
        status = "filled" if filled >= float(volume) - 1e-9 else "partially_filled"
        return {
            "status": status,
            "retcode": retcode,
            "comment": str(getattr(result, "comment", "") or ""),
            "order_id": int(getattr(result, "order", 0) or 0) or None,
            "deal_id": int(getattr(result, "deal", 0) or 0) or None,
            "position_id": int(position["ticket"]),
            "requested_volume": float(volume),
            "filled_volume": float(filled),
            "fill_price": float(getattr(result, "price", 0.0) or 0.0) or float(position["price_open"]),
        }

    def modify_sl(self, reference: str, sl: float) -> tuple[bool, str]:
        """Modify the SL of one physical leg position (ref = ticket str)."""
        ticket = int(reference)
        position = self._position(ticket)
        if position is None:
            return False, f"position {ticket} not found"
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": position["symbol"],
            "sl": float(sl),
            "tp": float(position.get("tp") or 0.0),
        }
        result = self.mt5.order_send(request)
        done = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        accepted = int(getattr(result, "retcode", -1) or -1) == done
        return accepted, str(getattr(result, "comment", "") or "")

    def query_sl(self, reference: str) -> float | None:
        position = self._position(int(reference))
        if position is None:
            return None
        return float(position.get("sl") or 0.0) or None

    # ------------------------------------------------------------------
    # Extra surface used by the executor / reconciliation
    # ------------------------------------------------------------------

    def close_position(self, reference: str, volume: float) -> dict[str, Any]:
        """Market-close one physical leg position (used by the compensation
        flow — an emergency execution event, never a TP/SL target)."""
        ticket = int(reference)
        position = self._position(ticket)
        if position is None:
            return {"status": "rejected", "retcode": -1, "comment": f"position {ticket} not found"}
        close_type = self.mt5.ORDER_TYPE_SELL if position["type"] == 0 else self.mt5.ORDER_TYPE_BUY
        snapshot = self.ctx.symbol_snapshot(position["symbol"])
        price = snapshot["bid"] if position["type"] == 0 else snapshot["ask"]
        close_volume = min(float(volume), float(position["volume"]))
        group_id = self.ctx.parse_comment(str(position.get("comment") or "")) or f"TG:{ticket}"
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": position["symbol"],
            "volume": close_volume,
            "type": close_type,
            "price": float(price),
            "deviation": 20,
            "magic": self.magic,
            "comment": self.ctx.build_comment(group_id, None),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        result = self.mt5.order_send(request)
        retcode = int(getattr(result, "retcode", -1) or -1)
        done = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        if retcode != done:
            return {
                "status": "rejected",
                "retcode": retcode,
                "comment": str(getattr(result, "comment", "") or ""),
                "order_id": getattr(result, "order", None),
            }
        return {
            "status": "filled",
            "retcode": retcode,
            "comment": str(getattr(result, "comment", "") or ""),
            "order_id": int(getattr(result, "order", 0) or 0) or None,
            "deal_id": int(getattr(result, "deal", 0) or 0) or None,
            "fill_price": float(getattr(result, "price", 0.0) or 0.0),
            "filled_volume": float(getattr(result, "volume", 0.0) or 0.0),
        }

    def query_position(self, reference: str) -> dict[str, Any] | None:
        return self._position(int(reference))

    def query_positions_by_magic(self) -> list[dict[str, Any]]:
        raw = self.mt5.positions_get() or []
        out = []
        for pos in raw:
            if int(getattr(pos, "magic", 0) or 0) != self.magic:
                continue
            out.append(self._as_dict(pos))
        return out

    def query_deals(self, position_ticket: int) -> list[dict[str, Any]]:
        raw = self.mt5.history_deals_get(position=position_ticket) or []
        return [self._deal_dict(deal) for deal in raw]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _position(self, ticket: int) -> dict[str, Any] | None:
        raw = self.mt5.positions_get(ticket=ticket)
        if not raw:
            return None
        return self._as_dict(raw[0])

    def _resolve_position(self, spec: TradeGroupSpec, leg: int) -> dict[str, Any] | None:
        """Find the just-opened position by magic + comment leg token.

        Leg-token match first (``L:<group>-L<n>``); only when the broker
        truncated the comment do we fall back to any position of the group.
        ``query_positions_by_magic`` returns normalized dicts, so dict access
        (``pos.get``) is used — ``getattr`` would silently yield "" for dicts.
        """
        expected_leg = new_leg_id(spec.group_id, leg)
        for pos in self.query_positions_by_magic():
            comment = str(pos.get("comment", "") or "")
            if f"L:{expected_leg}" in comment:
                return pos
        for pos in self.query_positions_by_magic():
            comment = str(pos.get("comment", "") or "")
            if self.ctx.parse_comment(comment) == spec.group_id:
                return pos
        return None

    def _as_dict(self, pos) -> dict[str, Any]:
        return {
            "ticket": int(getattr(pos, "ticket", 0)),
            "symbol": str(getattr(pos, "symbol", "")),
            "type": int(getattr(pos, "type", 0)),
            "volume": float(getattr(pos, "volume", 0.0)),
            "price_open": float(getattr(pos, "price_open", 0.0)),
            "price_current": float(getattr(pos, "price_current", 0.0)),
            "sl": float(getattr(pos, "sl", 0.0) or 0.0) or None,
            "tp": float(getattr(pos, "tp", 0.0) or 0.0) or None,
            "magic": int(getattr(pos, "magic", 0) or 0),
            "comment": str(getattr(pos, "comment", "") or ""),
        }

    def _deal_dict(self, deal) -> dict[str, Any]:
        return {
            "ticket": int(getattr(deal, "ticket", 0)),
            "position": int(getattr(deal, "position_id", 0) or getattr(deal, "position", 0) or 0),
            "symbol": str(getattr(deal, "symbol", "")),
            "type": int(getattr(deal, "type", 0)),
            "entry": int(getattr(deal, "entry", 0)),
            "price": float(getattr(deal, "price", 0.0)),
            "volume": float(getattr(deal, "volume", 0.0)),
            "time": int(getattr(deal, "time", 0)),
            "magic": int(getattr(deal, "magic", 0) or 0),
            "comment": str(getattr(deal, "comment", "") or ""),
        }
