"""
Shared test builder for cfg dicts, RealtimePipeline, signals, positions, and
risk configs.  Used by challenge/tests, realtime/tests, scripts/tests to
avoid duplicating stub/harness code.

All builders accept **overrides so individual tests can tweak one field
without constructing the whole dict from scratch.

Usage in tests
--------------
    from tests.builder import build_cfg, build_pipeline, build_signal

    def test_something():
        cfg = build_cfg(asset_timeframe={"BTCUSD": "M5"})
        pipe = build_pipeline(cfg=cfg, asset="BTCUSD")
        sig = build_signal(bias="long", symbol="XAUUSD")

Or via pytest fixtures (conftest.py wraps these).
"""

from __future__ import annotations

import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Path injection (tests may live in any subdirectory)
# ---------------------------------------------------------------------------
_PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)


# ---------------------------------------------------------------------------
# cfg builder
# ---------------------------------------------------------------------------
def build_cfg(
    asset_overrides: dict[str, dict] | None = None,
    asset_timeframe: dict[str, str] | None = None,
    data_mode: str = "mock",
    stealth_enabled: bool = False,
    **global_overrides,
) -> dict:
    """Build a test-safe cfg dict.  Sensible defaults for all 5 assets.

    Parameters
    ----------
    asset_overrides : extra per-asset keys merged into each asset section
    asset_timeframe : per-asset timeframe override, e.g. {"BTCUSD": "M5"}
    data_mode : "mock" (default) or "live"
    stealth_enabled : enable stealth execution engine
    **global_overrides : top-level cfg keys, e.g. require_provenance_manifest=False
    """
    assets = {}
    for key, mt5_sym, tf in [
        ("XAUUSD", "GOLD", "M15"),
        ("XAGUSD", "SILVER", "M15"),
        ("BTCUSD", "BTCUSD", "M5"),
        ("EURUSD", "EURUSD", "H1"),
        ("GBPUSD", "GBPUSD", "H1"),
    ]:
        asset_cfg: dict[str, Any] = {
            "enabled": True,
            "mt5_symbol": mt5_sym,
            "model_path": f"output/models/{key.lower()}_direction_model.joblib",
        }
        if asset_timeframe and key in asset_timeframe:
            asset_cfg["timeframe"] = asset_timeframe[key]
        if asset_overrides and key in asset_overrides:
            asset_cfg.update(asset_overrides[key])
        assets[key] = asset_cfg

    cfg: dict[str, Any] = {
        "general": {"db_path": "data/market_data_mt5.sqlite"},
        "market_data": {
            "provider": "mt5",
            "timeframe": "M5",
            "server_time_offset_hours": "auto",
            "server_time_offset_hours_fallback": 3,
        },
        "sessions": {
            "newyork": {"start_utc": "13:30", "end_utc": "19:55"},
            "london": {"start_utc": "08:00", "end_utc": "13:30"},
            "asia": {"start_utc": "00:00", "end_utc": "08:00"},
        },
        "assets": assets,
        "model": {
            "type": "xgboost",
            "train_ratio": 0.8,
        },
        "labeling": {"event": "barrier"},
        "backtest": {
            "walk_forward": {
                "train_window_days": 300,
                "test_window_days": 50,
                "step_days": 50,
                "embargo_candles": 36,
            }
        },
        "validation": {
            "require_provenance_manifest": False,
        },
        "deploy_guard": {
            "enabled": True,
            "primary_metric": "expectancy",
            "fallback_metrics": ["sharpe_ratio", "win_rate", "total_pnl"],
            "min_trades": 20,
            "tolerance": 0.0,
        },
        "challenge": {
            "enabled": False,
            "stealth": {"enabled": stealth_enabled},
        },
    }
    cfg.update(global_overrides)
    return cfg


# ---------------------------------------------------------------------------
# RealtimePipeline builder
# ---------------------------------------------------------------------------
def build_pipeline(
    cfg: dict | None = None,
    asset: str = "XAUUSD",
    data_mode: str = "mock",
    model_path: str | None = None,
) -> "RealtimePipeline":
    """Build a RealtimePipeline with test-safe defaults."""
    from realtime.pipeline import RealtimePipeline

    if cfg is None:
        cfg = build_cfg()
    if model_path is None:
        model_path = cfg["assets"].get(asset, {}).get("model_path")
    return RealtimePipeline(cfg=cfg, model_path=model_path, data_mode=data_mode)


# ---------------------------------------------------------------------------
# Signal dict builder
# ---------------------------------------------------------------------------
def build_signal(
    bias: str = "no_trade",
    symbol: str = "XAUUSD",
    confidence: float = 0.0,
    entry_price: float = 2500.0,
    stop: float = 2490.0,
    tp: float = 2520.0,
    **overrides,
) -> dict:
    """Build a signal dict matching the pipeline/gate contract."""
    sig = {
        "signal_id": "test-signal-001",
        "signal_state": "confirmed" if bias != "no_trade" else "no_trade",
        "strategy_version": "test-v1",
        "strategy_spec_hash": "abc123",
        "config_hash": "def456",
        "model_hash": "deadbeef",
        "feature_snapshot_hash": None,
        "setup_timeframe": "M15",
        "context_timeframes": ["M15", "H1"],
        "expires_at_utc": 9999999999,
        "target_legs": [{"price": tp, "close_ratio": 1 / 3, "label": "TP1"}],
        "confirmation_predicates": ["candle_closed", "regime_allowed", "session_allowed", "ensemble_gate_passed"],
        "confirmed_by": "systematic:ensemble" if bias != "no_trade" else None,
        "confirmation_time_utc": 9999999999 if bias != "no_trade" else None,
        "bias": bias,
        "confidence": confidence,
        "entry_zone": [entry_price - 1, entry_price + 1] if bias != "no_trade" else None,
        "invalidation": stop if bias != "no_trade" else None,
        "targets": [tp] if bias != "no_trade" else None,
        "step": 4.0,
        "reasoning_summary": f"test {bias}",
        "regime": "range",
        "timestamp_utc": 9999999999,
        "session": "newyork",
        "features": {},
        "book_gate": None,
    }
    sig.update(overrides)
    return sig


# ---------------------------------------------------------------------------
# Position dict builder
# ---------------------------------------------------------------------------
def build_position(
    symbol: str = "XAUUSD",
    side: str = "long",
    qty: float = 1.0,
    entry: float = 2500.0,
    stop: float = 2490.0,
    tp: float = 2520.0,
    remaining_shares: float | None = None,
    **overrides,
) -> dict:
    """Build a position dict matching the stealth engine / runner contract."""
    pos = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "tp": tp,
        "remaining_shares": remaining_shares or qty,
        "already_partialed": False,
        "current_price": entry,
    }
    pos.update(overrides)
    return pos


# ---------------------------------------------------------------------------
# Risk config builder
# ---------------------------------------------------------------------------
def build_risk(
    per_trade_risk_pct: float = 1.0,
    max_open_positions: int = 3,
    daily_loss_stop: float = 50.0,
    overall_max_loss: float = 100.0,
    **overrides,
) -> dict:
    """Build a risk config section."""
    r = {
        "per_trade_risk_pct": per_trade_risk_pct,
        "max_open_positions": max_open_positions,
        "daily_loss_stop": daily_loss_stop,
        "overall_max_loss": overall_max_loss,
    }
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------
# Risk object builder (object with attributes, not just a dict)
# ---------------------------------------------------------------------------
def build_risk_object(
    max_open_positions: int = 3,
    daily_loss_stop: float = 50.0,
    per_trade_risk_usd: float = 10.0,
    position_size_value: int = 5,
):
    """Build an object with risk attributes + position_size() method.

    Used by challenge runner tests that need risk.position_size(price, equity).
    """

    class _Risk:
        pass

    r = _Risk()
    r.max_open_positions = max_open_positions
    r.daily_loss_stop = daily_loss_stop
    r.per_trade_risk_usd = per_trade_risk_usd
    r.position_size_value = position_size_value

    def _position_size(self, price, equity):
        return self.position_size_value

    r.position_size = _position_size.__get__(r, type(r))
    return r


# ---------------------------------------------------------------------------
# Stub / fake classes for MT5, predictor, etc.
# ---------------------------------------------------------------------------
class StubPredictor:
    """Minimal predictor stub that returns neutral probabilities.

    Parameters match _FakePredictor in realtime/tests/test_dashboard_honesty.py
    so those tests can migrate here.
    """

    def __init__(self, p_long: float = 0.5, feature_cols: list[str] | None = None):
        self._p_long = p_long
        self.feature_cols = feature_cols or ["f0", "f1", "f2"]
        self.metadata = {"model_hash": "stub_hash_000"}

    def predict_proba(self, X):
        import pandas as pd

        n = len(X)
        return pd.DataFrame(
            {
                "p_long": [self._p_long] * n,
                "p_short": [1 - self._p_long] * n,
            },
            index=getattr(X, "index", None),
        )


class StubMT5:
    """Minimal MetaTrader5 shim for tests."""

    def __init__(self):
        self._balance = 10000.0
        self._positions = []

    def account_info(self):
        class _Info:
            balance = 10000.0
            equity = 10000.0
            profit = 0.0
            login = 99999
            server = "test"

        return _Info()

    def positions_get(self):
        return self._positions

    def history_deals_get(self, *a, **kw):
        return []


class StubConnector:
    """Minimal HashHedgeConnector stub for challenge runner tests.

    Records all calls in lists (orders, closes, stops, partials) so tests
    can assert on what was executed without touching a real broker.
    """

    def __init__(self):
        self.orders: list[tuple] = []
        self.closes: list[tuple] = []
        self.stops: list[tuple] = []
        self.partials: list[tuple] = []

    def place_order(self, symbol, side, qty, *args, **kwargs):
        self.orders.append((symbol, side, qty))
        return True

    def close_position(self, symbol, qty=None):
        self.closes.append((symbol, qty))
        return True

    def close_partial(self, symbol, qty):
        self.partials.append((symbol, qty))
        return True

    def modify_stop(self, ticket, stop):
        self.stops.append((ticket, stop))
        return True

    def get_positions(self):
        return []
