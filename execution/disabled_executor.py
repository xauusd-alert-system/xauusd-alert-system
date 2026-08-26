"""DisabledExecutor — the ONLY executor wired into signal-only profiles.

ТЗ §5/§6.4: in profile ``us_stocks_challenge`` (and ``replay``) no order may
ever leave the system. DisabledExecutor logs the full order intent as an
audit trail and then raises :class:`ExecutionDisabledError`, so an accidental
wiring bug surfaces loudly instead of silently placing a trade. It is a hard
technical barrier, not a UI toggle.

Legacy profiles never call this module: they keep their existing trader
stack (execution/mt5_trader.py and adapters), which is guarded at startup by
``usstocks.guards.assert_auto_trading_allowed``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger("execution.disabled")


class ExecutionDisabledError(RuntimeError):
    """Raised on ANY submit attempt while execution is disabled."""


@dataclass
class OrderRequest:
    """Market-order intent handed to an Executor (never sent anywhere)."""

    symbol: str
    side: str                      # "buy" | "sell"
    qty: float
    order_type: str = "market"     # market | limit
    price: Optional[float] = None  # required for limit orders
    ref: str = ""                  # correlation id (signal/journal id)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Executor(Protocol):
    """ТЗ §5 mandatory interface; the only implementation allowed in
    signal-only profiles is DisabledExecutor."""

    def submit(self, order: OrderRequest) -> None: ...


class DisabledExecutor:
    """Logs and rejects every submit attempt."""

    def __init__(self, reason: str = "execution.mode=disabled"):
        self.reason = reason

    def submit(self, order: OrderRequest) -> None:
        payload = order.to_dict() if isinstance(order, OrderRequest) else dict(order)
        logger.warning(
            "ORDER BLOCKED (%s): %s", self.reason, payload,
        )
        raise ExecutionDisabledError(
            f"Execution is disabled ({self.reason}); refusing to submit order "
            f"{payload.get('symbol')} {payload.get('side')} {payload.get('qty')} "
            f"(ref={payload.get('ref')}). us_stocks_challenge is signal-only: "
            "execute manually in the terminal."
        )


def executor_for_profile(profile: str) -> Executor:
    """Return the executor for a profile.

    Signal-only profiles get DisabledExecutor. Legacy profiles are rejected
    here on purpose — they own their trader stack and must not be silently
    re-wired through this factory.
    """
    from config.loader import SIGNAL_ONLY_PROFILES

    if profile in SIGNAL_ONLY_PROFILES:
        return DisabledExecutor(reason=f"profile={profile}")
    raise ValueError(
        f"executor_for_profile() only serves signal-only profiles "
        f"{SIGNAL_ONLY_PROFILES}; profile {profile!r} manages its own execution."
    )
