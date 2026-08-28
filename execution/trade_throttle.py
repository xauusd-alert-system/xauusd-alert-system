"""DEPRECATED shim — legacy ``TradeThrottle`` moved to ``risk/legacy_throttle.py``.

P2-10 separation of responsibilities:
    - daily limits / circuit breaker  → ``risk/limits.py`` (single source);
    - rate-based throttling (N orders per minute) → ``risk/throttle.py``;
    - loss-streak cooldown / hard stop / risk step-down → THIS legacy class
      (deprecated; absorbed into the engine as an optional gate and deleted
      in the cleanup phase, Фаза 7).

The class is re-exported unchanged so existing imports and tests keep
working without modification.
"""

import warnings

from risk.legacy_throttle import TradeThrottle  # noqa: F401

warnings.warn(
    "execution.trade_throttle is a deprecated shim; the new rate-based throttle lives in 'risk.throttle' (P2-10)",
    DeprecationWarning,
    stacklevel=2,
)
