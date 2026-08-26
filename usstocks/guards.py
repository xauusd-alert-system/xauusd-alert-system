"""Startup interlocks for signal-only profiles (docs/MIGRATION_PLAN.md, Stage B).

Every entry point that can reach real money / order routing MUST call
``assert_auto_trading_allowed(<source>)`` as the first statement of its
``main()``. Under a signal-only profile this refuses to start the process,
which makes automated trading *technically impossible* in profile
``us_stocks_challenge`` rather than merely hidden behind a UI toggle.

Legacy profiles (forex_legacy / crypto_legacy — the historical default when
no PROFILE env/CLI value is set) pass through unchanged, so existing
behaviour is preserved.
"""
from __future__ import annotations

import logging
import sys

from config.loader import get_profile, is_signal_only

logger = logging.getLogger("usstocks.guards")

#: Process exit code used when an executable module refuses to start under a
#: signal-only profile. Distinct from 1 so scripts can tell "refused by
#: policy" apart from generic crashes.
EXIT_SIGNAL_ONLY = 2


class SignalOnlyViolation(RuntimeError):
    """Internal marker; the guard converts it into ``SystemExit(2)``."""


def assert_auto_trading_allowed(source: str = "") -> None:
    """Refuse to run under a signal-only profile.

    Raises ``SystemExit(EXIT_SIGNAL_ONLY)`` (after logging) when the active
    profile forbids auto-execution. Returns silently otherwise.
    """
    profile = get_profile()
    if not is_signal_only(profile):
        return
    msg = (
        f"AUTO-TRADING REFUSED (profile={profile}): {source or 'this entry point'} "
        "can route orders. Profiles us_stocks_challenge/replay are SIGNAL-ONLY: "
        "scanner + risk control + Telegram signals + manual confirmation. "
        "For MT5/browser automation explicitly start with "
        "--profile forex_legacy (or crypto_legacy)."
    )
    logger.critical(msg)
    print(msg, file=sys.stderr)
    raise SystemExit(EXIT_SIGNAL_ONLY)


def require_signal_only(source: str = "") -> str:
    """Inverse guard for the usstocks scanner stack itself.

    Returns the active profile name when it IS signal-only; raises
    ``SystemExit(EXIT_SIGNAL_ONLY)`` otherwise. Used by `python -m
    usstocks.*` runners so they can never be hijacked into a legacy profile.
    """
    profile = get_profile()
    if not is_signal_only(profile):
        msg = (
            f"REFUSED (profile={profile}): {source or 'the usstocks runner'} is "
            "signal-only and must run under --profile us_stocks_challenge "
            "(or replay)."
        )
        logger.critical(msg)
        print(msg, file=sys.stderr)
        raise SystemExit(EXIT_SIGNAL_ONLY)
    return profile
