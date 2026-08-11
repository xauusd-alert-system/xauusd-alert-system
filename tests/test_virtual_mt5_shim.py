"""
FILE 25: Tests for the virtual MT5 shim + VirtualState + MarketSimulator
integration contract used by the *real, unmodified* trading stack.

Covers the exact MT5 API surface that ``execution/mt5_trader.py``,
``execution/risk_manager.py`` and ``data/mt5_provider.py`` depend on:

  - ``mt5.order_send``  (TRADE_ACTION_DEAL open / SLTP modify / DEAL close)
  - ``mt5.positions_get`` / ``history_deals_get`` / ``account_info``
  - ``mt5.symbol_info`` / ``symbol_info_tick``
  - ``mt5.copy_rates_from_pos`` -> numpy structured array with a "time" field
    compatible with ``data.mt5_provider._normalize_rates``
  - PnL/equity math (contract size based) used by the trader's reports

The shim is injected onto sys.path exactly the way ``scripts/run_simulation.py``
does, so ``import MetaTrader5 as mt5`` resolves to the fake package.
"""

from __future__ import annotations

import os
import sys

# --- Replicate the 2-line sys.path injection from run_simulation.py so the
# --- fake MetaTrader5 package shadows any real (uninstalled) one.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)
_shim_dir = os.path.join(_PROJECT_ROOT, "simulation", "mt5_shim")
if _shim_dir not in sys.path:
    sys.path.insert(0, _shim_dir)

import numpy as np
import pandas as pd
import pytest

import MetaTrader5 as mt5
from simulation.simulator import MarketSimulator, load_simulation_config
from simulation.virtual_state import VirtualState, DEAL_ENTRY_IN, DEAL_ENTRY_OUT
from data import mt5_provider
from execution.mt5_trader import positions_get_by_magic


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def cfg():
    """Slim simulation config for fast, deterministic tests."""
    base = {
        "initial_price": 2400.0,
        "tick_duration_seconds": 5,
        "bar_interval_ticks": 12,
        "m5_bar_interval_ticks": 60,
        "num_noise_traders": 10,
        "num_market_makers": 2,
        "num_trend_followers": 2,
        "num_mean_reversion": 2,
        "num_fundamental": 1,
        "noise_lambda": 0.3,
        "noise_lognormal_mu": 1.5,
        "noise_lognormal_sigma": 0.5,
        "noise_min_size": 0.01,
        "noise_max_size": 5.0,
        "mm_spread_offset_pct": 0.0003,
        "mm_max_inventory": 100.0,
        "fundamental_impact_factor": 0.5,
        "news_shock_std": 0.002,
        "news_mean_arrival_ticks": 500,
        "circuit_breaker_pct": 0.15,
        "spread_ask_offset": 0.30,
        "spread_bid_offset": 0.30,
        "slippage_points": 5,
        "virtual_balance": 10000.0,
        "virtual_magic": 777111,
        "symbol_overrides": {
            "XAUUSD": {
                "digits": 2,
                "point": 0.01,
                "trade_stops_level": 50,
                "trade_freeze_level": 0,
                "trade_contract_size": 100.0,
                "volume_min": 0.01,
                "volume_step": 0.01,
            }
        },
    }
    return base


@pytest.fixture()
def world(cfg):
    """Warmed-up (virtual) world: simulator + VirtualState wired into the shim."""
    sim = MarketSimulator(cfg=cfg, seed=42)
    # Build bar history with a plain step() (2 M1 bars -> 2x rebuilt M5 bars) so
    # copy_rates returns data. Note: warm_up() clears the aggregator at the end
    # (resets start_tick), which would leave zero history for copy_rates.
    sim.step(120)
    state = VirtualState(cfg)
    mt5._inject(state, sim, cfg)
    return state, sim


# ----------------------------------------------------------------------
# order_send: open (DEAL) / modify (SLTP) / close (DEAL+position)
# ----------------------------------------------------------------------
def test_order_send_deal_opens_position(world):
    state, sim = world
    before = state.account_info().balance

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": "XAUUSD",
        "volume": 0.10,
        "type": mt5.ORDER_TYPE_BUY,  # BUY
        "price": 2400.0,
        "sl": 2390.0,
        "tp": 2430.0,
        "deviation": 20,
        "magic": 777111,
        "comment": "test-open",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)

    assert result.retcode == mt5.TRADE_RETCODE_DONE == 10009
    # Ticket-faithful to real MT5: result.order is a *distinct order ticket*
    # (small, starting at 1), NOT the position ticket. Decoding the rest of the
    # result is the caller's job:
    assert 1 <= result.order  # order ticket namespace (real MT5 behaves the same)
    assert result.deal >= 200001  # deal ticket namespace
    assert result.volume == 0.1

    # --- HIGH 22 path: resolve the genuine position ticket via positions_get(),
    # --- exactly as execution/mt5_trader.execute_signal does in production.
    positions = positions_get_by_magic(symbol="XAUUSD", magic=777111)
    assert positions is not None
    assert len(positions) == 1
    pos = positions[0]
    assert pos.ticket != result.order  # order ticket != position ticket
    assert pos.symbol == "XAUUSD"
    assert pos.type == mt5.POSITION_TYPE_BUY
    assert pos.volume == 0.10
    assert pos.price_open == pytest.approx(2400.0)
    assert pos.sl == pytest.approx(2390.0)
    assert pos.tp == pytest.approx(2430.0)
    assert pos.magic == 777111
    assert state.account_info().balance == pytest.approx(before)  # no balance delta on open


def test_order_send_returns_distinct_order_and_position_tickets(world):
    """Ticket-faithful contract: result.order (order ticket) is NEVER usable as a
    position id -- the two tickets live in separate namespaces, exactly like a real
    MT5 terminal. Callers must resolve positions via positions_get() (HIGH 22)."""
    state, _sim = world

    # First open.
    res1 = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": 0.10,
            "type": mt5.ORDER_TYPE_BUY,
            "price": 2400.0,
            "sl": 0.0,
            "tp": 0.0,
            "magic": 777111,
            "comment": "test-open-1",
        }
    )
    assert res1.retcode == mt5.TRADE_RETCODE_DONE
    # Order ticket is a small monotonic counter (not the 100001+ position space).
    assert 1 <= res1.order < 100001

    pos = positions_get_by_magic(symbol="XAUUSD", magic=777111)[0]
    assert pos.ticket != res1.order
    assert pos.ticket >= 100001  # position ticket namespace
    assert res1.deal >= 200001  # deal ticket namespace

    # The order ticket is NOT a valid position id.
    assert state.get_position(res1.order) is None

    # Second open -> a NEW, distinct order ticket (monotonic++).
    res2 = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": 0.05,
            "type": mt5.ORDER_TYPE_SELL,
            "price": 2410.0,
            "sl": 0.0,
            "tp": 0.0,
            "magic": 777111,
            "comment": "test-open-2",
        }
    )
    assert res2.retcode == mt5.TRADE_RETCODE_DONE
    assert res2.order != res1.order
    assert res2.order > res1.order
    assert res2.deal != res1.deal

    # Two open positions, resolved purely via positions_get.
    positions = positions_get_by_magic(symbol="XAUUSD", magic=777111)
    assert len(positions) == 2

    # Every position ticket is distinct from every order ticket returned so far.
    order_tickets = {res1.order, res2.order}
    position_tickets = {p.ticket for p in positions}
    assert order_tickets.isdisjoint(position_tickets)


def test_order_send_sltp_modifies_position(world):
    mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": 0.05,
            "type": mt5.ORDER_TYPE_BUY,
            "price": 2400.0,
            "sl": 2390.0,
            "tp": 2430.0,
            "magic": 777111,
            "comment": "test-open",
        }
    )
    pos = mt5.positions_get()[0]

    mod = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol": "XAUUSD",
            "sl": 2395.0,
            "tp": 2440.0,
            "magic": 777111,
        }
    )
    assert mod.retcode == mt5.TRADE_RETCODE_DONE
    refreshed = mt5.positions_get()[0]
    assert refreshed.sl == pytest.approx(2395.0)
    assert refreshed.tp == pytest.approx(2440.0)


def test_order_send_deal_with_position_closes_and_updates_balance(world):
    state, sim = world
    # Open BUY 0.10 @ 2400.
    res = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": 0.10,
            "type": mt5.ORDER_TYPE_BUY,
            "price": 2400.0,
            "sl": 0.0,
            "tp": 0.0,
            "magic": 777111,
            "comment": "test-open",
        }
    )
    # Order ticket != position ticket; resolve the real position id like
    # execution/mt5_trader.execute_signal does (HIGH 22).
    pos_id = positions_get_by_magic(symbol="XAUUSD", magic=777111)[0].ticket
    balance_before = state.balance

    # Close partial 0.05 @ 2410 -> +$50 on 0.05 lots * 100 contract * $10 move.
    close = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": pos_id,  # MT5 "position" id means close/reverse
            "symbol": "XAUUSD",
            "volume": 0.05,
            "type": mt5.ORDER_TYPE_SELL,
            "price": 2410.0,
            "magic": 777111,
            "comment": "test-close-1",
        }
    )
    assert close.retcode == mt5.TRADE_RETCODE_DONE
    assert close.volume == 0.05

    # Remaining 0.05 lots still open.
    positions = mt5.positions_get()
    assert len(positions) == 1
    assert positions[0].volume == pytest.approx(0.05)
    assert state.balance == pytest.approx(balance_before + 50.0)

    # Close the rest @ 2410 -> another +$50.
    mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": pos_id,
            "symbol": "XAUUSD",
            "volume": 0.05,
            "type": mt5.ORDER_TYPE_SELL,
            "price": 2410.0,
            "magic": 777111,
            "comment": "test-close-2",
        }
    )
    assert mt5.positions_get() is None or len(mt5.positions_get()) == 0
    assert state.balance == pytest.approx(balance_before + 100.0)


# ----------------------------------------------------------------------
# History deals: IN / OUT entries
# ----------------------------------------------------------------------
def test_history_deals_get_returns_in_and_out(world):
    state, sim = world
    res = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": 0.10,
            "type": mt5.ORDER_TYPE_BUY,
            "price": 2400.0,
            "sl": 0.0,
            "tp": 0.0,
            "magic": 777111,
            "comment": "test-open",
        }
    )
    # Order ticket != position ticket; resolve the real position id via
    # positions_get (this is exactly the production HIGH 22 code path).
    pos_id = positions_get_by_magic(symbol="XAUUSD", magic=777111)[0].ticket

    mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": pos_id,
            "symbol": "XAUUSD",
            "volume": 0.10,
            "type": mt5.ORDER_TYPE_SELL,
            "price": 2410.0,
            "magic": 777111,
            "comment": "test-close",
        }
    )

    deals = mt5.history_deals_get(position=pos_id)
    assert deals is not None
    assert len(deals) == 2
    in_deal, out_deal = deals[0], deals[1]
    assert in_deal.entry == DEAL_ENTRY_IN
    assert in_deal.profit == 0.0
    assert in_deal.price == pytest.approx(2400.0)
    assert out_deal.entry == DEAL_ENTRY_OUT
    assert out_deal.profit == pytest.approx(100.0)  # 0.10 lots * 100 contract * $10
    assert out_deal.price == pytest.approx(2410.0)


# ----------------------------------------------------------------------
# account_info: equity = balance + floating PnL
# ----------------------------------------------------------------------
def test_account_info_equity_includes_floating_pnl(world):
    state, sim = world
    res = mt5.order_send(
        {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "XAUUSD",
            "volume": 0.10,
            "type": mt5.ORDER_TYPE_BUY,
            "price": 2400.0,
            "sl": 0.0,
            "tp": 0.0,
            "magic": 777111,
            "comment": "test-open",
        }
    )
    # Order ticket != position ticket; resolve via positions_get (HIGH 22).
    pos = positions_get_by_magic(symbol="XAUUSD", magic=777111)[0]

    # At the open price floating PnL = 0, equity == balance.
    pos.price_current = 2400.0
    account = mt5.account_info()
    assert account.balance == pytest.approx(10000.0)
    assert account.equity == pytest.approx(10000.0)

    # Move price to 2420 -> +$200 floating on 0.10 * 100 * $20.
    pos.price_current = 2420.0
    account = mt5.account_info()
    assert account.equity == pytest.approx(10000.0 + 200.0)


# ----------------------------------------------------------------------
# symbol_info / symbol_info_tick
# ----------------------------------------------------------------------
def test_symbol_info_and_tick(world):
    info = mt5.symbol_info("XAUUSD")
    assert info is not None
    assert info.digits == 2
    assert info.point == pytest.approx(0.01)
    assert info.trade_stops_level == 50
    assert info.trade_contract_size == pytest.approx(100.0)
    assert info.volume_min == pytest.approx(0.01)
    assert info.volume_step == pytest.approx(0.01)

    tick = mt5.symbol_info_tick("XAUUSD")
    assert tick is not None
    assert callable(getattr(tick, "bid", None)) is False or getattr(tick, "bid", None) is not None
    assert tick.ask >= tick.bid
    assert 1000.0 < tick.bid < 4000.0  # plausible XAU level


# ----------------------------------------------------------------------
# copy_rates_from_pos -> _normalize_rates compatibility
# ----------------------------------------------------------------------
def test_copy_rates_from_pos_structured_array_mt5_provider_compat(world):
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 1, 5)
    assert rates is not None
    assert len(rates) > 0
    assert rates.dtype.names == (
        "time",
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "spread",
        "real_volume",
    )
    # "time" field must be present and int-like (this is what the trader reads).
    assert "time" in rates.dtype.names
    assert rates[0]["time"] > 0

    # The exact path used by data/mt5_provider.fetch_closed_candles.
    df = mt5_provider._normalize_rates(rates)
    assert isinstance(df, pd.DataFrame)
    # N10: spread/real_volume are now preserved (they were dropped before),
    # so the column set is the required core plus the optional broker fields.
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    assert all(c in df.columns for c in required)
    assert "spread" in df.columns
    assert "real_volume" in df.columns
    assert len(df) == len(rates)
    assert df["close"].iloc[-1] > 0


def test_unwarmed_simulator_returns_none_for_rates(world):
    """Before warming up there are no closed bars, so rates is None/empty."""
    # Rebuild a fresh (un-warmed) simulator to confirm the None path.
    cfg = world[0].cfg or load_simulation_config()
    sim = MarketSimulator(cfg=cfg, seed=1)
    state = VirtualState(cfg)
    mt5._inject(state, sim, cfg)
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 1, 1)
    # start_pos=1 on an empty history returns None (MT5 semantics: no data).
    assert rates is None


# ----------------------------------------------------------------------
# Regression tests: run_simulation / run_bot wiring (live-loop integration)
# ----------------------------------------------------------------------

def test_run_simulation_imports_plain_mt5_module():
    """The entry points must inject state into the SAME module object that
    `import MetaTrader5 as mt5` in execution/data resolves to. A dotted
    `from simulation.mt5_shim import MetaTrader5` creates a second module
    object and _inject() would be invisible to the protected modules
    (run_loop would never see new bars -> no trades at all)."""
    import scripts.run_simulation as rs
    assert rs.mt5 is mt5  # mt5 here is the plain top-level MetaTrader5 shim


def test_build_virtual_cfg_registers_mt5_symbol_names():
    """build_virtual_cfg must extend symbol_overrides with the MT5 symbol
    names from the main config (GOLD, SILVER, ...) so the trader's
    validate_symbol(mt5_symbol) succeeds against the virtual terminal.

    Shadow assets (XAGUSD `enabled: false` per the 2026-08-07 quant audit)
    are intentionally NOT registered — the simulator mirrors the live symbol
    set, so SILVER must be absent while the 4 live symbols are present.
    """
    from scripts.run_simulation import build_virtual_cfg
    cfg = build_virtual_cfg()
    overrides = cfg["symbol_overrides"]
    assert "GOLD" in overrides
    assert "BITCOIN" in overrides
    assert "EURUSD" in overrides
    assert "GBPUSD" in overrides
    # Shadow asset: XAGUSD is disabled -> its MT5 symbol must not be tradable
    assert "SILVER" not in overrides
    assert "XAGUSD" not in overrides
    # MT5 name inherits the asset-key params (e.g. gold contract size 100).
    assert overrides["GOLD"].get("trade_contract_size") == overrides.get("XAUUSD", {}).get(
        "trade_contract_size", 100.0
    )


def test_circuit_breaker_anchor_rolls_on_bar_close(cfg):
    """The CB anchor must re-anchor on every closed M5 bar (rolling anchor),
    so a slow multi-bar drift cannot freeze the simulation permanently."""
    sim = MarketSimulator(cfg=cfg, seed=1)
    sim.warm_up(n_ticks=240)
    for _ in range(3):
        sim.advance_to_next_m5_bar(max_ticks=120)
    assert sim.paused is False
    # After the last bar close the anchor tracks the current mid exactly.
    assert sim._cb_anchor == pytest.approx(sim.current_mid_price, rel=1e-9)


def test_positions_get_rejects_magic_like_real_api():
    """N3/W9: the shim must mirror the REAL MT5 API, which has no `magic`
    parameter. Passing magic= raises TypeError here exactly as it would on a
    live terminal, so production code can't silently regress to the old call.
    """
    import MetaTrader5 as mt5_real
    with pytest.raises(TypeError):
        mt5_real.positions_get(symbol="XAUUSD", magic=777111)


def test_positions_get_by_magic_filters_in_python():
    """W9: the production helper positions_get_by_magic filters by pos.magic in
    Python (no magic= kwarg), so it works against both the shim and the real API.
    """
    from execution.mt5_trader import positions_get_by_magic
    # No state initialized -> returns None (mirrors real API "no positions").
    assert positions_get_by_magic(symbol="XAUUSD", magic=777111) is None or \
        positions_get_by_magic(symbol="XAUUSD", magic=777111) == []
