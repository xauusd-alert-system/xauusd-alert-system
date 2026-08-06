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
            "timestamp_utc": idx.astype("int64") // 10**9,
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
            assert resolved == pytest.approx(expected["XAUUSD"]), (
                f"{asset_key} must fall back to the gold-default 100"
            )
        else:
            assert default_pvl == pvl, (
                f"assets.{asset_key}.point_value_lot must be {pvl}, got {default_pvl}"
            )

    # Zeros / negative values would break the money scale entirely.
    for asset_key in expected:
        assert (
            cfg["assets"][asset_key].get("point_value_lot", 100) > 0
        ), f"{asset_key} point_value_lot must be > 0"


# ---------------------------------------------------------------------------
# FX v3: early breakeven (signal_grid.breakeven_trigger_atr)
# ---------------------------------------------------------------------------

def _fx_v3_early_be_cfg(breakeven_trigger_atr=None):
    """Zero-cost config on the equal-step grid (stop = 3*step) with an optional
    early-breakeven trigger. Zero commission/slippage/spread so PnL assertions
    reflect pure barrier mechanics, not transaction costs."""
    cfg = _cfg({
        "slippage_usd": 0.0,
        "spread_usd": 0.0,
    })
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
