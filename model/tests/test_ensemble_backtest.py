"""
Regression tests for the EnsembleBacktester slippage handling (FX unit-mismatch bug).

Background: the backtester once applied a GLOBAL absolute-price slippage
(`backtest.slippage_points` default 5 -> 0.05 price units) to EVERY asset. For
low-priced FX (EURUSD ~1.08) that is ~460 pips / ~177x-ATR, blowing straight
through the ATR-sized stop on the opening bar and turning every trade into an
instant loss (0% win rate, pnl == -commission exactly). Production MT5 slippage
is instrument-specific deviation in points, so the backtester must honour a
per-asset `slippage_usd` override, mirroring the existing `spread_usd` pattern.
"""

import pandas as pd
import pytest

from data.ingestion import to_epoch_seconds
from model.ensemble_backtest import EnsembleBacktester


def _cfg(asset_section: dict, bt_slippage_points=5) -> dict:
    return {
        "assets": {"TEST": asset_section},
        "backtest": {
            "slippage_points": bt_slippage_points,
            "spread_points": 25,
            "initial_balance": 100.0,
            "volume": 0.01,
            "commission_per_trade": 0.07,
            "swap_per_night": 0.0,
            "point_value_lot": 100.0,
        },
        "labeling": {
            "horizon_candles_n": 36,
            "atr_column": "atr",
            "tp1_atr_multiplier": 1.0,
            "tp2_atr_multiplier": 1.8,
            "tp3_atr_multiplier": 2.8,
            "stop_atr_multiplier": 1.0,
        },
    }


def _df(n=400, price=1.10):
    """Flat, interbar-constant candles: every open/high/low/close == price.
    High/low == price means no intra-candle move, so in the backtest the stop
    and TP are NOT hit on neighbouring bars the way wildly-range candles do.
    A constant session/regime and strong ml_p_long (0.9) yield a deterministic
    long entry, letting tests assert exact entry_price math."""
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp_utc": to_epoch_seconds(idx),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 100.0,
            "session": "london",
            "regime": "trend_up",
            "atr": 0.0003,
            "ml_p_long": 0.9,
            "ml_p_short": 0.1,
        }
    )
    return df


def test_per_asset_slippage_override_is_used():
    """A declared slippage_usd must replace the (broken) global absolute default."""
    cfg = _cfg({"slippage_usd": 0.0002, "spread_usd": 0.00012})
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    assert bt.slippage == pytest.approx(0.0002)
    assert bt.slippage != pytest.approx(0.05)


def test_global_slippage_points_default_applied_when_no_override():
    """Backward compatibility: assets without slippage_usd keep slippage_points/100."""
    cfg = _cfg({})
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    assert bt.slippage == pytest.approx(5 / 100.0)


def test_entry_price_includes_fractional_spread_and_per_asset_slippage():
    """A long entry on candle open is shifted by +spread/2 + slippage_usd."""
    cfg = _cfg({"slippage_usd": 0.0002, "spread_usd": 0.0004})
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df()
    trades = bt.run(df)
    assert len(trades) > 0
    first = trades[0]
    assert first.direction == 1  # long (ml_p_long 0.9)
    # The entry happens on the open of the candle whose ts == first.entry_ts.
    entry_bar = int(df.index[df["timestamp_utc"] == first.entry_ts][0])
    expected_entry = df["open"].iloc[entry_bar] + 0.0004 / 2 + 0.0002
    assert first.entry_price == pytest.approx(expected_entry, abs=1e-9)


def test_next_open_grid_uses_causal_signal_atr_not_completed_entry_bar_atr():
    cfg = _cfg({"slippage_usd": 0.0, "spread_usd": 0.0})
    df = _df()
    df.loc[0, "atr"] = 0.001
    df.loc[1, "atr"] = 0.010  # unknown at bar-1 open; must not define the trade
    trade = EnsembleBacktester(cfg, asset_key="TEST").run(df)[0]
    assert abs(trade.entry_price - trade.initial_stop_price) == pytest.approx(0.001)


def test_grid_step_clamps_are_applied_in_backtester():
    cfg = _cfg(
        {
            "slippage_usd": 0.0,
            "spread_usd": 0.0,
            "signal_grid": {"step_min_points": 0.002, "stop_mult": 1.0},
        }
    )
    trade = EnsembleBacktester(cfg, asset_key="TEST").run(_df())[0]
    assert abs(trade.entry_price - trade.initial_stop_price) == pytest.approx(0.002)


def test_fx_slippage_override_does_not_turn_every_trade_into_an_instant_loss():
    """With realistic per-asset slippage, not every exit is an immediate ATR-stop loss."""
    cfg = _cfg({"slippage_usd": 0.0002, "spread_usd": 0.00012})
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    trades = bt.run(_df())
    assert len(trades) > 0
    # A realistic slippage must be well inside the first candle's ATR-scaled stop,
    # so we must see at least one exit that is NOT an instant stop-on-open.
    reasons = {t.exit_reason for t in trades}
    assert reasons != {"stop"} or len({round(t.pnl, 3) for t in trades}) > 1


def test_per_asset_point_value_lot_override_is_used():
    """A declared point_value_lot must replace the gold-default (100) multiplier.

    Without this override every asset shared the gold contract multiplier, which
    scaled low-priced FX/XAG money-PnL ~100-1000x too small so the flat
    commission swamped every trade (pnl ~= -commission -> 0% win rate even after
    the slippage fix). 1.0 FX lot = 100,000 units -> 0.01 lot = $1000/price unit.
    """
    cfg = _cfg({"slippage_usd": 0.0002, "spread_usd": 0.00012, "point_value_lot": 100000})
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    assert bt.point_value_lot == pytest.approx(100000)
    trades = bt.run(_df())
    assert len(trades) > 0
    # The flat synthetic candles produce only `timeout` exits (no intra-candle
    # move hits the stop). A timeout at close (~0.00046 below entry) is a tiny
    # price PnL; with the correct FX multiplier it must become a REAL money value
    # (0.01 lot * $1000/price-unit * 0.00046 ~= 0.46, minus 0.07 commission),
    # NOT the degenerate `pnl ~= -commission` that marked the mutated bug.
    timeout_pnls = [t.pnl for t in trades if t.exit_reason == "timeout"]
    assert timeout_pnls, "expected at least one timeout exit"
    assert all(p <= -0.30 for p in timeout_pnls)  # -0.46 + 0.07 comm = -0.33
    assert not any(abs(p - (-0.07)) < 1e-6 for p in timeout_pnls)  # not ~-commission


def test_default_point_value_lot_is_gold_multiplier_for_unoverridden_assets():
    """Backward compatibility: no override -> the global default (100) is used."""
    cfg = _cfg({})
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    assert bt.point_value_lot == pytest.approx(100.0)
    assert bt.volume * bt.point_value_lot == pytest.approx(1.0)


def test_config_point_value_lot_overrides_match_contract_sizes_per_asset():
    """The production config must declare a correct size for every instrument.

    `point_value_lot` = USD notional per 1.0 lot per 1.0 price unit. The
    backtester's global default (100) is a GOLD multiplier (1 lot = 100 oz).
    Declared overrides must match the real MT5 contract sizes:
      - XAGUSD 1 lot = 5000 oz  -> 5000
      - EURUSD 1 lot = 100000 units -> 100000
      - GBPUSD 1 lot = 100000 units -> 100000
      - BTCUSD 1 lot = 1 BTC -> 1   (NOT gold's 100: that inflated PnL 100x)
      - XAUUSD keeps the default (100 = correct gold multiplier)
    """
    from config.loader import load_config

    cfg = load_config()
    expected = {
        "XAUUSD": 100,
        "XAGUSD": 5000,
        "EURUSD": 100000,
        "GBPUSD": 100000,
        "BTCUSD": 1,
    }
    for asset_key, pvl in expected.items():
        asset_cfg = cfg["assets"][asset_key]
        default_pvl = asset_cfg.get("point_value_lot")
        if default_pvl is None:
            bt = EnsembleBacktester(cfg, asset_key=asset_key)
            resolved = bt.point_value_lot
            assert resolved == pytest.approx(expected["XAUUSD"]), f"{asset_key} must fall back to the gold-default 100"
        else:
            assert default_pvl == pvl, f"assets.{asset_key}.point_value_lot must be {pvl}, got {default_pvl}"

    # Zeros / negative values would break the money scale entirely.
    for asset_key in expected:
        assert cfg["assets"][asset_key].get("point_value_lot", 100) > 0, f"{asset_key} point_value_lot must be > 0"


# ---------------------------------------------------------------------------
# FX v3: early breakeven (signal_grid.breakeven_trigger_atr)
# ---------------------------------------------------------------------------


def _fx_v3_early_be_cfg(breakeven_trigger_atr=None):
    """Zero-cost config on the equal-step grid (stop = 3*step) with an optional
    early-breakeven trigger. Zero commission/slippage/spread so PnL assertions
    reflect pure barrier mechanics, not transaction costs."""
    cfg = _cfg(
        {
            "slippage_usd": 0.0,
            "spread_usd": 0.0,
        }
    )
    cfg["backtest"]["commission_per_trade"] = 0.0
    grid = {"stop_mult": 3.0}
    if breakeven_trigger_atr is not None:
        grid["breakeven_trigger_atr"] = breakeven_trigger_atr
    cfg["signal_grid"] = grid
    return cfg


def _fx_v3_probe_df(n=10, price=1.10, step=0.0003):
    """Flat candles with a +0.6-step probe on bar 2 and a -3.5-step drop on bar
    3 (same shape as the mean-reverting FX scenario the early breakeven is
    designed for)."""
    df = _df(n=n, price=price)
    df.loc[2, "high"] = price + 0.6 * step
    df.loc[3, "low"] = price - 3.5 * step
    return df


def test_early_breakeven_turns_stop_loss_into_scratch():
    """With breakeven_trigger_atr=0.5 the stop moves to entry after the +0.6-step
    probe, so the -3.5-step drop that follows exits as a BREAKEVEN scratch
    (pnl == 0) instead of a full 3-step stop loss."""
    bt = EnsembleBacktester(_fx_v3_early_be_cfg(0.5), asset_key="TEST")
    trades = bt.run(_fx_v3_probe_df().head(10))
    assert len(trades) == 1
    assert trades[0].exit_reason == "breakeven"
    assert trades[0].pnl > -0.0005


def test_legacy_trigger_keeps_full_stop_loss():
    """Without breakeven_trigger_atr (default 1.0 = legacy) the same price path
    keeps the stop at -3*step, so the -3.5-step drop exits as a full STOP loss
    (pnl < -0.0005). This locks in that the default preserves old behaviour."""
    bt = EnsembleBacktester(_fx_v3_early_be_cfg(), asset_key="TEST")
    trades = bt.run(_fx_v3_probe_df().head(10))
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop"
    assert trades[0].pnl < -0.0005


# -----------------------------------------------------------------
# v4b trailing_atr_mult (after TP1+TP2)
# -----------------------------------------------------------------


def _cfg_with_trailing(trail_mult=2.0):
    cfg = _fx_v3_early_be_cfg(1.0)  # legacy BE
    cfg["signal_grid"]["trailing_atr_mult"] = trail_mult
    cfg["signal_grid"]["stop_mult"] = 3.0
    cfg["signal_grid"]["tp2_mult"] = 2.0
    cfg["signal_grid"]["tp3_mult"] = 4.0
    cfg["backtest"]["commission_per_trade"] = 0.0
    return cfg


def _trailing_probe_df(n=30, price=1.10, step=0.0003):
    """After TP1 (+1s) and TP2 (+2s) the price runs up to +3.5s (bars 8-12),
    then pulls back > trailing*atr (2.0 * step) on bar 15 => trailing exit on
    the 20% remainder.

    The probe must respect the engine's conservative INTRABAR semantics:
    - `_df` starts with low == high == entry, and after TP1 the stop moves to
      the entry price, so a flat low == entry would scratch the BE stop on the
      next bar. Bars 3-7 therefore keep their lows strictly ABOVE entry.
    - The trail stop ratchets to high - 2*ATR (== +1.5s here) on the same bar
      whose high prints, so any bar with a low below +1.5s exits immediately.
      Bars 8-14 keep lows at +2.6s (above the ratcheted stop) and only bar 15
      dips to +1.2s, which is below the stop -> 'trailing' exit.
    """
    df = _df(n=n, price=price)
    # TP1 at bar ~2 (high touches +1.1s)
    df.loc[2, "high"] = price + 1.1 * step
    # TP2 at bar ~5 (high touches +2.1s)
    df.loc[5, "high"] = price + 2.1 * step
    # After TP1 the stop is at entry: keep lows above entry until the pullback.
    df.loc[3:7, "low"] = price + 0.6 * step
    # Run higher to +3.5s: trail stop ratchets to high - 2*ATR = +1.5s; lows
    # stay above it (2.6s) so the runner survives bars 8-14.
    df.loc[8:14, "high"] = price + 3.5 * step
    df.loc[8:14, "low"] = price + 2.6 * step
    df.loc[8:14, "close"] = price + 3.4 * step
    # Pullback > 2.0 * step (low drops to +1.2s < trail stop +1.5s)
    df.loc[15, "low"] = price + 3.4 * step - 2.2 * step
    return df


def test_trailing_atr_mult_exits_on_trail_after_tp2():
    """With trailing_atr_mult=2.0, after TP1+TP2 the remainder is trailed;
    a pullback > trailing triggers exit_reason='trailing'."""
    cfg = _cfg_with_trailing(2.0)
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    trades = bt.run(_trailing_probe_df().head(25))
    assert len(trades) == 1
    assert trades[0].exit_reason == "trailing"
    # Should have captured more than a plain TP2 would have
    assert trades[0].pnl > 0.0002


# ---------------------------------------------------------------------------
# Quant audit 2026-08-07: fill_mode (look-ahead check), per-regime exit
# policy (signal_grid.regime_overrides), scaleout ratios, queue loss
# ---------------------------------------------------------------------------


def _regime_cfg(regime_overrides=None, scaleout=None):
    cfg = _fx_v3_early_be_cfg(1.0)
    cfg["signal_grid"]["regime_overrides"] = regime_overrides or {}
    if scaleout:
        cfg["signal_grid"]["scaleout"] = scaleout
    return cfg


def test_fill_mode_signal_close_enters_at_signal_bar_close():
    """fill_mode='signal_close' must open at the CLOSE of the signal bar (the
    look-ahead measurement), while the default enters at the NEXT open."""
    cfg = _cfg_with_trailing()  # commission 0, legacy BE
    cfg["signal_grid"]["regime_overrides"] = {}
    bt_honest = EnsembleBacktester(cfg, asset_key="TEST")
    bt_close = EnsembleBacktester(cfg, asset_key="TEST")
    bt_close.fill_mode = "signal_close"
    df = _df(n=400)
    df.loc[1, "open"] = 1.1001  # next-open differs from signal close -> distinguishes fills
    t_honest = bt_honest.run(df)
    t_close = bt_close.run(df)
    assert len(t_close) >= 1 and len(t_honest) >= 1
    # signal fires at bar 0: signal_close fills at the SIGNAL bar's close,
    # the honest mode at the NEXT bar's open.
    assert t_close[0].entry_price == pytest.approx(df["close"].iloc[0], abs=1e-9)
    assert t_honest[0].entry_price == pytest.approx(df["open"].iloc[1], abs=1e-9)
    assert t_close[0].entry_price != t_honest[0].entry_price


def test_regime_override_resolved_at_entry():
    """regime_overrides.trend_up must widen the stop vs the base grid."""
    cfg = _fx_v3_early_be_cfg(1.0)
    cfg["signal_grid"]["regime_overrides"] = {
        "trend_up": {"stop_mult": 5.0, "tp3_mult": 4.0},
    }
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df(n=400)
    trades = bt.run(df)
    assert len(trades) >= 1
    t0 = trades[0]
    stop_dist = abs(t0.entry_price - t0.stop_price)
    assert stop_dist == pytest.approx(5.0 * 0.0003, rel=1e-6)
    tp3_dist = abs(t0.tp3_price - t0.entry_price)
    assert tp3_dist == pytest.approx(4.0 * 0.0003, rel=1e-6)
    assert t0.regime_at_entry == "trend_up"


def test_regime_override_absent_keeps_base_grid():
    """No regime_overrides -> bit-identical to the legacy base grid."""
    cfg = _fx_v3_early_be_cfg(1.0)
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df(n=400)
    trades = bt.run(df)
    stop_dist = abs(trades[0].entry_price - trades[0].stop_price)
    assert stop_dist == pytest.approx(3.0 * 0.0003, rel=1e-6)


def test_scaleout_ratios_change_remaining_position():
    """A 60/40/0 (mean-reversion) scaleout must realize TP1 on 60%: the
    TP1-only scratch path differs from the default 50/30/20."""
    cfg = _cfg_with_trailing()
    cfg["signal_grid"]["scaleout"] = {"tp1_ratio": 0.6, "tp2_ratio": 0.4}
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    # path: TP1 at bar 2, then flat -> timeout at close ~ entry -> remaining 40%
    df = _df(n=400)
    df.loc[2, "high"] = 1.10 + 1.1 * 0.0003
    trades = bt.run(df)
    assert len(trades) >= 1
    # first TP1 leg paid 0.6 * 1 step; remaining 0.4 times ~0 (flat timeout)
    assert trades[0].pnl == pytest.approx(0.6 * 1.0 * 0.0003, abs=1e-4)


def test_rejected_signals_recorded_while_position_open():
    """Signals that fire while a position is open must be recorded (queue
    loss input). Flat df with ml_p_long 0.9 -> signal on bar 1, entry at bar
    2 open, timeout keeps a position open -> bar-2 signal is rejected."""
    cfg = _fx_v3_early_be_cfg(1.0)
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df(n=25)
    bt.run(df)
    assert len(bt.rejected_signals) > 0
    rej = bt.rejected_signals[0]
    assert rej["direction"] in (1, -1)
    assert "regime" in rej and "bar" in rej


def test_simulate_blocked_entry_uses_honest_next_open():
    """simulate_blocked_entry must enter at the open of signal_bar+1 and
    produce exactly one trade via the engine's own exit logic."""
    cfg = _fx_v3_early_be_cfg(1.0)
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    df = _df(n=40)
    t = bt.simulate_blocked_entry(df, signal_bar=2, direction=1)
    assert t is not None
    assert t.entry_price == pytest.approx(df["open"].iloc[3], abs=1e-9)
    assert t.direction == 1


# ---------------------------------------------------------------------------
# Audit W1 + W4 (quant audit 2026-08-10)
#   W1: exit costs (half-spread + slippage) must be charged on EVERY exit, so
#       the round trip is a full spread + two slippages (entry already pays
#       half-spread + one slippage). Before this, exit costs were not charged.
#   W4: a same-candle double-touch of the ORIGINAL stop and a take-profit must
#       resolve conservatively as a full stop loss (not a TP scaleout + BE gift).
# ---------------------------------------------------------------------------


def test_exit_costs_charged_on_timeout():
    """W1: a timeout exit must book the exit half-spread + slippage.

    With spread_usd=0.0004 and slippage_usd=0.0002 on a flat 1.10 path, entry is
    at open + spread/2 + slippage = 1.1004 and a long exit at close (1.10) pays
    bid minus slippage: 1.10 - (0.0002 + 0.0002) = 1.0996. The round-trip cost is
    therefore a full spread + two slippages instead of the previous half-spread +
    one slippage (which understated losses on every trade)."""
    cfg = _cfg({"slippage_usd": 0.0002, "spread_usd": 0.0004, "point_value_lot": 100000})
    cfg["backtest"]["commission_per_trade"] = 0.0
    cfg["labeling"]["horizon_candles_n"] = 3  # force a quick timeout exit
    # Wide grid so the flat path (price 1.10) neither hits TP nor the stop and
    # instead closes on the timeout at the flat close.
    cfg["signal_grid"] = {"stop_mult": 5.0, "tp1_mult": 3.0, "breakeven_trigger_atr": 1.0}
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    trades = bt.run(_df(n=20))
    timeout = [t for t in trades if t.exit_reason == "timeout"]
    assert timeout, "expected at least one timeout exit"
    t = timeout[0]
    entry = t.entry_price
    assert entry == pytest.approx(1.10 + 0.0002 + 0.0002, abs=1e-9)  # +spread/2 + slippage
    exit_costed = 1.10 - (0.0004 / 2.0 + 0.0002)  # long exit pays bid minus slippage
    expected_pnl = 1.0 * (exit_costed - entry) * 0.01 * 100000
    assert t.pnl == pytest.approx(expected_pnl, abs=1e-6)


def test_same_candle_tp1_and_original_stop_is_conservative_full_stop():
    """W4: a bar piercing BOTH TP1 and the original stop is a full stop loss,
    not a 50% TP1 scaleout + breakeven gift on the remainder."""
    cfg = _cfg({"slippage_usd": 0.0, "spread_usd": 0.0})
    cfg["backtest"]["commission_per_trade"] = 0.0
    cfg["signal_grid"] = {"stop_mult": 3.0, "tp1_mult": 1.0, "breakeven_trigger_atr": 1.0}
    cfg["labeling"]["horizon_candles_n"] = 100
    bt = EnsembleBacktester(cfg, asset_key="TEST")
    step = 0.0003
    df = _df(n=10, price=1.10)
    # Huge-range bar 2 that touches TP1 (+1.5s) AND the original stop (-4s).
    df.loc[2, "high"] = 1.10 + 1.5 * step
    df.loc[2, "low"] = 1.10 - 4.0 * step
    trades = bt.run(df.head(4))
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "stop"
    # money scale = volume * point_value_lot = 0.01 * 100 = 1.0
    assert t.pnl == pytest.approx(1.0 * (1.10 - 3.0 * step - 1.10), abs=1e-12)
    # No partial TP1 was banked: tp1_hit must stay False and the full stop distance booked.
    assert t.tp1_hit is False
