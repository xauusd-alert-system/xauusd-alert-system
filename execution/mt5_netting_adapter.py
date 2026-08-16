"""
MT5 Netting adapter for TradeGroupSpec v1 (ТЗ P1.5 §13/§14/§19).

Netting accounts get ONE physical aggregate position + three VIRTUAL legs:

    L1 -> TP1 allocation (closed by a partial close at TP1)
    L2 -> TP2 allocation (closed by a partial close at TP2)
    L3 -> TP3 allocation (final close at TP3)

The broker position carries SL=spec.sl and TP=spec.tp3 (final target), so a
broker-side TP close of the aggregate is a valid terminal event for the whole
group. All virtual-leg state lives in ``TradeGroupSpec`` / the group store; the
adapter never presents the group as three independent MT5 positions.

BE is a SINGLE SL modification on the aggregate position — two sequential
opposite SL modifies for virtual legs are forbidden (ТЗ §19).
"""
from __future__ import annotations

from typing import Any

from execution.mt5_common import MT5BrokerContext
from execution.trade_group import TradeGroupSpec, new_leg_id


class MT5NettingDriver:
    """Single-aggregate-position driver implementing the GroupDriver surface."""

    mode = "demo"
    account_mode = "netting"

    def __init__(self, mt5, magic: int = 777111, comment_limit: int = 63):
        self.ctx = MT5BrokerContext(mt5, magic=magic, comment_limit=comment_limit)
        self.mt5 = mt5
        self.magic = int(magic)
        # virtual ref -> aggregate ticket, rebuilt on restart from broker state
        self._ref_map: dict[str, int] = {}

    # ------------------------------------------------------------------
    # GroupDriver surface
    # ------------------------------------------------------------------

    def submit_leg(self, spec: TradeGroupSpec, leg: int, volume: float) -> dict[str, Any]:
        """Leg 1 opens the aggregate position with the GROUP TOTAL volume (the
        legs are virtual allocations of it); legs 2/3 are virtual (ТЗ §13)."""
        if leg == 1:
            # the physical position carries the WHOLE group volume; each leg's
            # volume is the portion closed at its TP
            open_volume = float(spec.risk.total_volume)
            snapshot = self.ctx.symbol_snapshot(spec.broker_symbol)
            price = snapshot["ask"] if spec.side == "long" else snapshot["bid"]
            order_type = self.mt5.ORDER_TYPE_BUY if spec.side == "long" \
                else self.mt5.ORDER_TYPE_SELL
            request = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": spec.broker_symbol,
                "volume": open_volume,
                "type": order_type,
                "price": float(price),
                "deviation": 20,
                "magic": self.magic,
                "comment": self.ctx.build_comment(spec.group_id, 1),
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self.mt5.ORDER_FILLING_IOC,
                "sl": float(spec.geometry.sl),
                "tp": float(spec.geometry.tp3),   # final target on the aggregate
            }
            result = self.mt5.order_send(request)
            retcode = int(getattr(result, "retcode", -1) or -1)
            done = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
            if retcode != done:
                return {
                    "status": "rejected", "retcode": retcode,
                    "comment": str(getattr(result, "comment", "") or ""),
                    "order_id": getattr(result, "order", None),
                    "requested_volume": open_volume, "filled_volume": 0.0,
                    "fill_price": None,
                }
            position = self._resolve_aggregate(spec)
            if position is None:
                return {
                    "status": "rejected", "retcode": retcode,
                    "comment": "order accepted but position could not be resolved",
                    "order_id": getattr(result, "order", None),
                    "requested_volume": open_volume, "filled_volume": 0.0,
                    "fill_price": None,
                }
            ticket = int(position["ticket"])
            self._ref_map[new_leg_id(spec.group_id, 1)] = ticket
            self._ref_map[new_leg_id(spec.group_id, 2)] = ticket
            self._ref_map[new_leg_id(spec.group_id, 3)] = ticket
            filled = float(getattr(result, "volume", 0.0) or 0.0) or float(position["volume"])
            status = "filled" if filled >= open_volume - 1e-9 else "partially_filled"
            return {
                "status": status, "retcode": retcode,
                "comment": str(getattr(result, "comment", "") or ""),
                "order_id": int(getattr(result, "order", 0) or 0) or None,
                "deal_id": int(getattr(result, "deal", 0) or 0) or None,
                "position_id": ticket,
                "requested_volume": open_volume, "filled_volume": float(filled),
                "fill_price": float(getattr(result, "price", 0.0) or 0.0)
                or float(position["price_open"]),
                "virtual": False,
            }
        # virtual legs 2/3: no broker order, share the aggregate position
        ticket = self._ref_map.get(new_leg_id(spec.group_id, 1))
        if ticket is None:
            position = self._resolve_aggregate(spec)
            ticket = int(position["ticket"]) if position else 0
        if not ticket:
            return {
                "status": "rejected", "retcode": -1,
                "comment": "aggregate position missing for virtual leg",
                "requested_volume": float(volume), "filled_volume": 0.0,
            }
        self._ref_map[new_leg_id(spec.group_id, leg)] = ticket
        return {
            "status": "virtual", "retcode": 0,
            "comment": "virtual leg (netting)",
            "position_id": ticket,
            "requested_volume": float(volume), "filled_volume": 0.0,
            "fill_price": None,
            "virtual": True,
        }

    def modify_sl(self, reference: str, sl: float) -> tuple[bool, str]:
        """Single SL modify on the aggregate position (ТЗ §19)."""
        ticket = self._resolve_ticket(reference)
        position = self._position(ticket)
        if position is None:
            return False, f"aggregate position {ticket} not found"
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
        position = self._position(self._resolve_ticket(reference))
        if position is None:
            return None
        return float(position.get("sl") or 0.0) or None

    # ------------------------------------------------------------------
    # Extra surface used by the executor / reconciliation
    # ------------------------------------------------------------------

    def close_partial(self, reference: str, volume: float) -> dict[str, Any]:
        """Partial close of the aggregate position (TP1/TP2/TP3 legs)."""
        ticket = self._resolve_ticket(reference)
        position = self._position(ticket)
        if position is None:
            return {"status": "rejected", "retcode": -1,
                    "comment": f"aggregate position {ticket} not found"}
        close_type = self.mt5.ORDER_TYPE_SELL if position["type"] == 0 \
            else self.mt5.ORDER_TYPE_BUY
        snapshot = self.ctx.symbol_snapshot(position["symbol"])
        price = snapshot["bid"] if position["type"] == 0 else snapshot["ask"]
        group_id = self.ctx.parse_comment(position["comment"]) or f"TG:{position['ticket']}"
        close_comment = f"TG:{group_id}|CLOSE"
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": position["symbol"],
            "volume": float(volume),
            "type": close_type,
            "price": float(price),
            "deviation": 20,
            "magic": self.magic,
            "comment": close_comment[: self.ctx.comment_limit],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        result = self.mt5.order_send(request)
        retcode = int(getattr(result, "retcode", -1) or -1)
        done = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        if retcode != done:
            return {"status": "rejected", "retcode": retcode,
                    "comment": str(getattr(result, "comment", "") or ""),
                    "order_id": getattr(result, "order", None)}
        return {
            "status": "filled", "retcode": retcode,
            "comment": str(getattr(result, "comment", "") or ""),
            "order_id": int(getattr(result, "order", 0) or 0) or None,
            "deal_id": int(getattr(result, "deal", 0) or 0) or None,
            "fill_price": float(getattr(result, "price", 0.0) or 0.0),
            "filled_volume": float(getattr(result, "volume", 0.0) or 0.0),
        }

    def query_position(self, reference: str) -> dict[str, Any] | None:
        return self._position(self._resolve_ticket(reference))

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

    def _resolve_ticket(self, reference: str) -> int:
        """Resolve a virtual-leg ref to the aggregate ticket.

        Restart-safe: when the in-memory ref map is empty (fresh driver after a
        process restart), the aggregate position is re-discovered from broker
        state by magic + comment group id (ТЗ §29).
        """
        try:
            return int(reference)
        except (TypeError, ValueError):
            pass
        if reference in self._ref_map:
            return int(self._ref_map[reference] or 0)
        group_id = str(reference).rsplit("-L", 1)[0]
        for pos in self.query_positions_by_magic():
            if self.ctx.parse_comment(str(pos.get("comment", "") or "")) == group_id:
                self._ref_map[reference] = int(pos["ticket"])
                return int(pos["ticket"])
        return 0

    def _resolve_aggregate(self, spec: TradeGroupSpec) -> dict[str, Any] | None:
        """Find the aggregate position by magic + comment group id (dict access:
        ``query_positions_by_magic`` returns normalized dicts)."""
        for pos in self.query_positions_by_magic():
            comment = str(pos.get("comment", "") or "")
            if self.ctx.parse_comment(comment) == spec.group_id:
                return pos
        return None

    def _position(self, ticket: int) -> dict[str, Any] | None:
        if not ticket:
            return None
        raw = self.mt5.positions_get(ticket=ticket)
        if not raw:
            return None
        return self._as_dict(raw[0])

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
