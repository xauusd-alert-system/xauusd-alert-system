"""
VirtualState: single source of truth for the virtual MT5 account.

It tracks virtual positions, history deals, account balance/equity and
monotonic ticket counters.  The MT5 shim (simulation/mt5_shim/MetaTrader5)
dispatches its ``order_send`` / ``positions_get`` / ``history_deals_get`` /
``account_info`` calls onto this state object, so the *real, unmodified*
MultiAssetMT5Trader logic runs against a simulated account.

PnL semantics match MT5 for these instruments:

    BUY :  pnl = (exit_price - entry_price) * volume * contract_size
    SELL:  pnl = (entry_price - exit_price) * volume * contract_size

Contract sizes come from symbol_overrides in simulation_config.yaml
(XAUUSD=100.0, BTCUSD=1.0), so a 0.01 lot XAU position moves $1 per $1
price move.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Match MetaTrader5.TRADE_RETCODE_DONE (10009) so retcode checks in
# execution/risk_manager.py and execution/mt5_trader.py pass unchanged.
TRADE_RETCODE_DONE: int = 10009
TRADE_RETCODE_INVALID_REQUEST: int = 10014
TRADE_RETCODE_REJECT: int = 10016

# Deal flags / entries used to distinguish open-vs-close records.
DEAL_ENTRY_IN: int = 0
DEAL_ENTRY_OUT: int = 1
DEAL_ENTRY_INOUT: int = 2

# Position tickets start at 100001, deal tickets at 200001 so they can never
# collide (mirrors realistic broker ticket spaces).
_POSITION_TICKET_START = 100001
_DEAL_TICKET_START = 200001


@dataclass
class VirtualTick:
    """Duck-typed replacement for MT5 symbol_info_tick()."""

    symbol: str
    bid: float
    ask: float
    time: int = 0

    @property
    def last(self) -> float:
        return round((self.bid + self.ask) / 2.0, 6)

    @property
    def volume(self) -> float:
        return 0.0

    def _as_dict(self) -> dict:
        return {
            "time": self.time,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "volume": self.volume,
            "symbol": self.symbol,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VirtualTick(symbol={self.symbol!r}, bid={self.bid}, "
            f"ask={self.ask}, time={self.time})"
        )


@dataclass
class VirtualSymbolInfo:
    """Duck-typed replacement for MT5 symbol_info()."""

    name: str
    digits: int
    point: float
    trade_stops_level: int
    trade_freeze_level: int
    trade_contract_size: float
    volume_min: float
    volume_step: float
    spread: float = 0.30
    tick_size: float = 0.01
    trade_tick_value: float = 1.0
    trade_mode: int = 0
    margin_initial: float = 0.0
    margin_maintenance: float = 0.0
    session_interest_rates: int = 0
    currency_base: str = "XAU"
    currency_profit: str = "USD"
    currency_margin: str = "USD"
    path: str = ""

    def __post_init__(self) -> None:
        self.path = self.name

    def _as_dict(self) -> dict:
        return {
            "name": self.name,
            "digits": self.digits,
            "point": self.point,
            "trade_stops_level": self.trade_stops_level,
            "trade_freeze_level": self.trade_freeze_level,
            "trade_contract_size": self.trade_contract_size,
            "volume_min": self.volume_min,
            "volume_step": self.volume_step,
            "spread": self.spread,
            "tick_size": self.tick_size,
            "trade_tick_value": self.trade_tick_value,
            "trade_mode": self.trade_mode,
            "margin_initial": self.margin_initial,
            "margin_maintenance": self.margin_maintenance,
            "currency_base": self.currency_base,
            "currency_profit": self.currency_profit,
            "currency_margin": self.currency_margin,
            "path": self.path,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"VirtualSymbolInfo(name={self.name!r}, digits={self.digits})"


@dataclass
class VirtualPosition:
    """Duck-typed replacement for an MT5 position."""

    ticket: int
    symbol: str
    type: int = 0  # 0 = BUY, 1 = SELL
    volume: float = 0.0
    price_open: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = 0
    time_update: int = 0
    price_current: float = 0.0
    swap: float = 0.0
    profit: float = 0.0

    @property
    def type_name(self) -> str:
        return "buy" if self.type == 0 else "sell"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VirtualPosition(ticket={self.ticket}, symbol={self.symbol!r}, "
            f"type={self.type_name}, volume={self.volume}, "
            f"open={self.price_open}, sl={self.sl}, tp={self.tp})"
        )


@dataclass
class VirtualDeal:
    """Duck-typed replacement for an MT5 deal record."""

    ticket: int
    position_id: int
    symbol: str
    type: int = 0  # 0 = BUY, 1 = SELL
    entry: int = DEAL_ENTRY_IN
    volume: float = 0.0
    price: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VirtualDeal(ticket={self.ticket}, pos={self.position_id}, "
            f"symbol={self.symbol!r}, profit={self.profit:.2f})"
        )


@dataclass
class VirtualAccountInfo:
    """Duck-typed replacement for MT5 account_info()."""

    login: int = 0
    server: str = "virtual"
    currency: str = "USD"
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    profit: float = 0.0
    leverage: int = 100
    trade_allowed: int = 1
    trade_expert: int = 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VirtualAccountInfo(balance={self.balance:.2f}, "
            f"equity={self.equity:.2f}, margin_free={self.margin_free:.2f})"
        )


@dataclass
class VirtualOrderResult:
    """Duck-typed replacement for the MT5 order_send() result."""

    retcode: int = TRADE_RETCODE_DONE
    order: int = 0
    comment: str = "Virtual done"
    deal: int = 0
    price: float = 0.0
    volume: float = 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VirtualOrderResult(retcode={self.retcode}, order={self.order}, "
            f"deal={self.deal}, comment={self.comment!r})"
        )


class VirtualState:
    """Holds the full simulated account + position/deal ledger."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.balance: float = float(cfg.get("virtual_balance", 10000.0))

        # Ticket counters.
        self._position_ticket = _POSITION_TICKET_START - 1
        self._deal_ticket = _DEAL_TICKET_START - 1
        # order_ticket counter (used for the "order" field of order_send).
        self._order_ticket = 0

        self.positions: dict[int, VirtualPosition] = {}
        self.deals: list[VirtualDeal] = []
        self.account = VirtualAccountInfo(
            login=1,
            server="virtual-mt5",
            currency="USD",
            balance=self.balance,
            equity=self.balance,
            leverage=int(cfg.get("virtual_leverage", 100)),
        )
        self.symbols: dict[str, VirtualSymbolInfo] = {}
        for sym, over in self._symbol_overrides().items():
            self.symbols[sym] = self._build_symbol_info(sym, over)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _symbol_overrides(self) -> dict:
        return dict(self.cfg.get("symbol_overrides", {}) or {})

    def _build_symbol_info(self, symbol: str, over: dict) -> VirtualSymbolInfo:
        return VirtualSymbolInfo(
            name=symbol,
            digits=int(over.get("digits", 2)),
            point=float(over.get("point", 0.01)),
            trade_stops_level=int(over.get("trade_stops_level", 50)),
            trade_freeze_level=int(over.get("trade_freeze_level", 0)),
            trade_contract_size=float(
                over.get("trade_contract_size", 100.0)
            ),
            volume_min=float(over.get("volume_min", 0.01)),
            volume_step=float(over.get("volume_step", 0.01)),
            spread=float(over.get("spread", 0.30)),
            tick_size=float(over.get("point", 0.01)),
        )

    def symbol_info(self, symbol: str) -> Optional[VirtualSymbolInfo]:
        return self.symbols.get(symbol)

    # ------------------------------------------------------------------
    # Tick / price feed
    # ------------------------------------------------------------------
    def make_tick(self, symbol: str, bid: float, ask: float, t: int = 0) -> VirtualTick:
        return VirtualTick(symbol=symbol, bid=bid, ask=ask, time=t)

    def _contract_size(self, symbol: str) -> float:
        over = self.symbol_info(symbol)
        return over.trade_contract_size if over else 100.0

    # ------------------------------------------------------------------
    # PnL helpers
    # ------------------------------------------------------------------
    def _compute_pnl(
        self, symbol: str, position_type: int, entry: float, exit_price: float,
        volume: float,
    ) -> float:
        """Realized or floating PnL for a (partial) lot volume."""
        contract = self._contract_size(symbol)
        if position_type == 0:  # BUY
            pnl = (exit_price - entry) * volume * contract
        else:  # SELL
            pnl = (entry - exit_price) * volume * contract
        return round(pnl, 2)

    def _compute_floating_pnl(self, pos: VirtualPosition, mark: float) -> float:
        return self._compute_pnl(
            pos.symbol, pos.type, pos.price_open, mark, pos.volume
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def account_info(self) -> VirtualAccountInfo:
        """Return the account with freshly computed equity / floating PnL."""
        floating = 0.0
        for pos in self.positions.values():
            try:
                floating += self._compute_floating_pnl(pos, pos.price_current)
            except Exception:
                continue
        self.account.balance = round(self.balance, 2)
        self.account.equity = round(self.balance + floating, 2)
        self.account.profit = round(floating, 2)
        self.account.margin_free = round(self.account.equity, 2)
        return self.account

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------
    def _next_position_ticket(self) -> int:
        self._position_ticket += 1
        return self._position_ticket

    def _next_deal_ticket(self) -> int:
        self._deal_ticket += 1
        return self._deal_ticket

    def _next_order_ticket(self) -> int:
        self._order_ticket += 1
        return self._order_ticket

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def open_position(
        self,
        symbol: str,
        position_type: int,
        volume: float,
        price: float,
        sl: float,
        tp: float,
        magic: int,
        comment: str = "",
        t: int = 0,
    ) -> tuple[VirtualPosition, VirtualDeal]:
        """Open a new position, create the IN deal and return (position, deal)."""
        ticket = self._next_position_ticket()
        pos = VirtualPosition(
            ticket=ticket,
            symbol=symbol,
            type=position_type,
            volume=round(volume, 2),
            price_open=round(price, 6),
            sl=round(sl, 6) if sl else 0.0,
            tp=round(tp, 6) if tp else 0.0,
            magic=int(magic),
            comment=comment,
            time=t,
            time_update=t,
            price_current=round(price, 6),
        )
        self.positions[ticket] = pos

        deal = VirtualDeal(
            ticket=self._next_deal_ticket(),
            position_id=ticket,
            symbol=symbol,
            type=position_type,
            entry=DEAL_ENTRY_IN,
            volume=pos.volume,
            price=pos.price_open,
            profit=0.0,
            swap=0.0,
            commission=0.0,
            magic=int(magic),
            comment=comment,
            time=t,
        )
        self.deals.append(deal)
        return pos, deal

    def close_partial(
        self,
        pos: VirtualPosition,
        volume: float,
        price: float,
        comment: str = "",
        t: int = 0,
    ) -> Optional[VirtualDeal]:
        """Close a (partial) volume of an open position at ``price``.

        Creates an OUT deal carrying realized PnL, updates balance and the
        position's remaining volume.  Cumulated swap is charged proportionally to
        the closed volume on every (partial) close and applied to the balance.
        When volume reaches (near) zero the position is removed.
        Returns the OUT deal or None on error.
        """
        if pos.ticket not in self.positions:
            return None
        if volume <= 0.0:
            return None

        volume = min(volume, pos.volume)
        if volume <= 1e-12:
            return None

        # Swap accrues on the position; charge the share matching this close.
        # On a full close we take the entire remaining swap (already scaled by
        # the volume ratio so no double counting occurs).
        close_type = 1 if pos.type == 0 else 0  # opposite side deal type
        profit = self._compute_pnl(
            pos.symbol, pos.type, pos.price_open, price, volume
        )
        swap_charge = round(pos.swap * (volume / pos.volume), 2)
        self.balance = round(self.balance + profit + swap_charge, 2)

        deal = VirtualDeal(
            ticket=self._next_deal_ticket(),
            position_id=pos.ticket,
            symbol=pos.symbol,
            type=close_type,
            entry=DEAL_ENTRY_OUT,
            volume=round(volume, 2),
            price=round(price, 6),
            profit=profit,
            swap=swap_charge,
            commission=0.0,
            magic=pos.magic,
            comment=comment,
            time=t,
        )
        self.deals.append(deal)

        remaining = round(pos.volume - volume, 2)
        # Keep the position's total accumulated swap value, but reflect that the
        # closed share has been charged, so a later (partial) close charges only
        # its own proportional share.
        pos.swap = round(pos.swap - swap_charge, 2)
        pos.volume = remaining
        if remaining <= 1e-12:
            del self.positions[pos.ticket]
        else:
            pos.time_update = t

        return deal

    def modify_sl_tp(self, pos: VirtualPosition, sl: float, tp: float) -> bool:
        """Modify the SL/TP of an open position."""
        if pos.ticket not in self.positions:
            return False
        pos.sl = round(sl, 6) if sl else 0.0
        pos.tp = round(tp, 6) if tp else 0.0
        return True

    def get_positions(
        self, symbol: Optional[str] = None, magic: Optional[int] = None
    ) -> list[VirtualPosition]:
        """Filtered list of open positions (MT5 positions_get semantics)."""
        result = []
        for pos in self.positions.values():
            if symbol is not None and pos.symbol != symbol:
                continue
            if magic is not None and pos.magic != int(magic):
                continue
            result.append(pos)
        return result

    def get_position(self, ticket: int) -> Optional[VirtualPosition]:
        return self.positions.get(int(ticket))

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def get_history_deals(
        self, position: Optional[int] = None
    ) -> list[VirtualDeal]:
        """Deals history, optionally filtered by position_id (ticket)."""
        if position is None:
            return list(self.deals)
        return [d for d in self.deals if d.position_id == int(position)]

    def reset(self) -> None:
        """Reset the virtual account to its initial state."""
        self.balance = float(self.cfg.get("virtual_balance", 10000.0))
        self._position_ticket = _POSITION_TICKET_START - 1
        self._deal_ticket = _DEAL_TICKET_START - 1
        self._order_ticket = 0
        self.positions.clear()
        self.deals.clear()
        self.account = VirtualAccountInfo(
            login=1,
            server="virtual-mt5",
            currency="USD",
            balance=self.balance,
            equity=self.balance,
            leverage=int(self.cfg.get("virtual_leverage", 100)),
        )
        self.symbols.clear()
        for sym, over in self._symbol_overrides().items():
            self.symbols[sym] = self._build_symbol_info(sym, over)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VirtualState(balance={self.balance:.2f}, "
            f"positions={len(self.positions)}, deals={len(self.deals)})"
        )
