"""Shared builder for ``object.__new__``-based trader harnesses.

The execution tests construct ``MultiAssetMT5Trader`` via ``object.__new__``
(bypassing ``__init__``, which would connect to MT5 / Telegram). As the trader
grows, the close detector, BE blocks and blackout paths touch more attributes
(``trade_throttle``, ``last_close_pnl``, ``signal_features``, ...); keeping
that attribute list in ONE place prevents the recurring
``'MultiAssetMT5Trader' object has no attribute X`` failures every time
production code adds a new dependency.

Every default is overridable via kwargs, so test-specific shapes (e.g.
``be_state`` as a dict vs a set) stay with the test.
"""
import os
import tempfile

from execution.mt5_trader import MultiAssetMT5Trader
from execution.trade_throttle import TradeThrottle


class FakeBot:
    """Captures Telegram messages instead of sending them."""

    def __init__(self):
        self.messages = []

    def send_text_message(self, text):
        self.messages.append(text)
        return True


def _tmp_path(name: str) -> str:
    return os.path.join(tempfile.gettempdir(), name)


def build_trader(**attrs) -> MultiAssetMT5Trader:
    """Construct a trader with default harness attributes.

    Pass any attribute as a keyword to override the default (e.g.
    ``active_trades={123: {...}}``, ``cfg={...}``, ``bot=my_fake_bot()``).
    """
    t = object.__new__(MultiAssetMT5Trader)
    defaults = {
        "cfg": {},
        "magic_number": 777111,
        "dry_run": False,
        "active_trades": {},
        "be_state": set(),                # close_notification: set of tickets
        "be_trigger_by_symbol": {},       # breakeven_legs: per-symbol triggers
        "trailing_atr_mult_by_symbol": {},
        "streak_losses": {},
        "signal_features": {},
        "last_close_pnl": {},
        # _append_trade_event falls back to strategy_identity when the signal
        # contract is empty (close detection path); provide a harmless stub.
        "strategy_identity": {
            "strategy_version": "test-harness",
            "config_hash": "test-harness",
        },
        "bot": FakeBot(),
        "trade_db_path": _tmp_path("harness_trades.sqlite"),
        "management_state_path": _tmp_path("harness_mgmt_state.json"),
        # Real throttle (faithful: the close detector calls on_trade_closed),
        # but pointed at a temp state file so tests never touch logs/.
        "trade_throttle": TradeThrottle(
            {}, state_path=_tmp_path("harness_throttle_state.json"),
        ),
        "_blackout_flattened": False,
    }
    for key, value in defaults.items():
        setattr(t, key, value)
    for key, value in attrs.items():
        setattr(t, key, value)
    return t
