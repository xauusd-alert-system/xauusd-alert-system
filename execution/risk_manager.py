"""DEPRECATED shim — implementation moved to the ``risk/`` package (ТЗ 8.5).

``InstitutionalRiskManager`` now lives in ``risk/compat.py`` (a thin compat
wrapper over ``risk.limits.RiskLimits`` + ``risk.state.RiskState``).

This module re-exports the public surface so existing imports
(``from execution.risk_manager import InstitutionalRiskManager``) keep
working unchanged. It will be deleted in the cleanup phase (Фаза 7) —
update imports to ``risk`` instead.
"""
import warnings
from datetime import datetime, timezone  # noqa: F401 — legacy test surface

from mt5_adapter.lazy import get_mt5_module
from risk.compat import InstitutionalRiskManager  # noqa: F401
from risk.limits import RiskLimits  # noqa: F401
from risk.state import RiskState  # noqa: F401

# ТЗ 8.6: raw module handle via the adapter (kept for backwards compat —
# tests patch ``rm.mt5.*`` and risk.limits shares the same module object).
mt5 = get_mt5_module()

warnings.warn(
    "execution.risk_manager is a deprecated shim; import from 'risk' instead",
    DeprecationWarning,
    stacklevel=2,
)
