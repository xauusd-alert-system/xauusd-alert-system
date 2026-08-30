"""DEPRECATED shim — implementation moved to the ``risk/`` package (ТЗ 8.5).

All sizing functions now live in ``risk/sizing.py`` (with P1-4:
``cluster_exposure_ok`` requires ``cluster_cap``/``total_cap`` explicitly).
This module re-exports the public surface so existing imports
(``from execution.risk_sizer import lots_for_risk, ...``) keep working
unchanged. It will be deleted in the cleanup phase (Фаза 7) — update
imports to ``risk.sizing`` instead.
"""

import warnings

from risk.sizing import (  # noqa: F401
    cluster_exposure_ok,
    drawdown_throttle,
    leverage_multiplier,
    lots_for_risk,
    risk_config,
    same_direction_cluster_penalty,
    trade_risk_pct,
    vol_target_scale,
)

warnings.warn(
    "execution.risk_sizer is a deprecated shim; import from 'risk.sizing' instead",
    DeprecationWarning,
    stacklevel=2,
)
