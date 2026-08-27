"""Lazy resolution of the low-level MT5 module through the adapter (ТЗ 8.6).

Production modules must not ``import MetaTrader5`` directly; they obtain the
raw module handle from here instead. The resolution mirrors the historical
behaviour exactly (the import result is cached by ``sys.modules`` and the
simulation shim is found through the normal import machinery when
``scripts/run_simulation.py`` has injected it onto ``sys.path``):

1. plain ``import MetaTrader5`` — the real package, or the virtual shim when
   it was placed on ``sys.path`` first;
2. fallback: the dotted ``simulation.mt5_shim.MetaTrader5`` (pre-existing
   fallback semantics of ``execution/broker_adapter.py``).
"""
from __future__ import annotations

from typing import Any


def get_mt5_module() -> Any:
    """Return the low-level MetaTrader5 module (real or shim).

    Raises ``ImportError`` when neither the package nor the simulation shim is
    importable — identical to a failing ``import MetaTrader5``."""
    try:
        import MetaTrader5  # noqa: PLC0415 — lazy by design

        return MetaTrader5
    except ImportError:
        pass
    try:
        # Pre-existing fallback (execution/broker_adapter.py): dotted import.
        from simulation.mt5_shim import MetaTrader5  # type: ignore
        return MetaTrader5
    except ImportError as exc:
        raise ImportError(
            "MetaTrader5 package is unavailable and no simulation shim "
            "found (mt5_adapter.lazy.get_mt5_module)") from exc
