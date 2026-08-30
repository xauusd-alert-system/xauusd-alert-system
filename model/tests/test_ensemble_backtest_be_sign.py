"""
Regression tests for the short-side breakeven sign bug (quant audit 2026-08-14).

The early-breakeven block computed its trigger level as

    be_level = entry_price + direction * be_trigger_mult * (tp1_price - entry_price)

`tp1_price - entry_price` already carries the side: it equals
`direction * atr * tp1_mult`. The extra `direction` factor therefore squared to
+1 and placed the trigger ABOVE the entry on both sides. On a long that is the
intended level. On a short it sits one TP1 distance on the LOSING side, while
the trigger test is `lows[i] <= be_level`, which the first bar after entry
satisfies essentially always. Every short had its stop pulled to the entry price
before price moved in its favour at all, and `exit_reason` then reported the
resulting adverse exits as "breakeven" scratches:

    engine protect%  = 96.82
    independent barrier scan on the same bars = 58.84
    175 of 191 real stops booked as scratches   (scripts/diag_entry_vs_label.py)

455 of 472 walk-forward trades were shorts, so the whole PnL curve was an
artefact of exit bookkeeping rather than of the barrier geometry. The bug
survived because every breakeven case in test_ensemble_backtest.py is long-only,
where the squared factor is invisible. These tests pin the short side down.

Why this file is worth its length (2026-08-14): it was written with the fix but
never merged, so when the fix was reverted the whole suite stayed green and the
next walk-forward run silently reproduced the buggy +8915 PnL. A fix without its
guard is a fix with a countdown on it.
"""

import pytest

from model.ensemble_backtest import EnsembleBacktester
from model.tests.test_ensemble_backtest import _cfg, _df

PRICE = 1.10
ATR = 0.0003  # the atr column value used by _df
# money scale = backtest.volume * point_value_lot = 0.01 * 100 = 1.0, so a price
# distance of 2 * ATR books as a PnL of -0.0006 exactly.


def _zero_cost_cfg(breakeven_trigger_atr=1.0, stop_mult=2.0):
    """Zero spread/slippage/commission so every assertion below reflects pure
    barrier mechanics. TP2/TP3 are pushed far out of reach of the probes."""
    cfg = _cfg({"slippage_usd": 0.0, "spread_usd": 0.0})
    cfg["backtest"]["commission_per_trade"] = 0.0
    cfg["signal_grid"] = {
        "stop_mult": stop_mult,
        "tp1_mult": 1.0,
        "tp2_mult": 5.0,
        "tp3_mult": 6.0,
        "breakeven_trigger_atr": breakeven_trigger_atr,
    }
    cfg["labeling"]["horizon_candles_n"] = 100  # no timeout inside the probe
    return cfg


def _short_df(n=10):
    """Mirror of `_df` for the short side: a trend_down regime plus
    ml_p_short 0.9 makes the rule vote and the model vote agree, so a short
    opens on the open of bar 1 (the signal fires on bar 0)."""
    df = _df(n=n, price=PRICE)
    df["regime"] = "trend_down"
    df["ml_p_long"] = 0.1
    df["ml_p_short"] = 0.9
    return df


def test_short_stop_is_not_relabelled_as_breakeven_without_a_favourable_move():
    """THE REGRESSION. The path never trades below the entry, so a short has
    earned nothing and the breakeven trigger must stay untouched. Bar 3 then
    runs up through the 2 x ATR stop: that is a full stop loss.

    Before the fix be_triggered went True on bar 1 (low == entry <= entry + 1
    ATR), the stop was pulled to the entry price and this exit was booked as
    exit_reason "breakeven" with pnl ~ 0.
    """
    bt = EnsembleBacktester(_zero_cost_cfg(breakeven_trigger_atr=1.0), asset_key="TEST")
    df = _short_df()
    df.loc[3, "high"] = PRICE + 2.1 * ATR
    trades = bt.run(df)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == -1
    assert t.entry_price == pytest.approx(PRICE, abs=1e-12)
    assert t.stop_price == pytest.approx(PRICE + 2.0 * ATR, rel=1e-9)
    assert t.tp1_hit is False
    assert t.exit_reason == "stop"
    assert t.pnl == pytest.approx(-2.0 * ATR, rel=1e-6)


def test_short_early_breakeven_still_scratches_after_a_real_favourable_move():
    """The feature itself must keep working on the short side. Bar 2 trades
    0.6 x ATR in favour: past the 0.5 x ATR trigger but short of TP1 (1.0 x
    ATR), so the stop moves to the entry without banking a scaleout, and the
    adverse run on bar 3 exits flat instead of at -2 x ATR."""
    bt = EnsembleBacktester(_zero_cost_cfg(breakeven_trigger_atr=0.5), asset_key="TEST")
    df = _short_df()
    df.loc[2, "low"] = PRICE - 0.6 * ATR
    df.loc[3, "high"] = PRICE + 2.1 * ATR
    trades = bt.run(df)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == -1
    assert t.tp1_hit is False
    assert t.exit_reason == "breakeven"
    assert t.pnl == pytest.approx(0.0, abs=1e-9)


def test_long_full_stop_is_unchanged_by_the_sign_fix():
    """Control: the long side was already correct and must stay bit-identical.
    No favourable move, then a drop through the 2 x ATR stop -> full stop."""
    bt = EnsembleBacktester(_zero_cost_cfg(breakeven_trigger_atr=1.0), asset_key="TEST")
    df = _df(n=10, price=PRICE)  # trend_up + ml_p_long 0.9 -> long
    df.loc[3, "low"] = PRICE - 2.1 * ATR
    trades = bt.run(df)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == 1
    assert t.stop_price == pytest.approx(PRICE - 2.0 * ATR, rel=1e-9)
    assert t.exit_reason == "stop"
    assert t.pnl == pytest.approx(-2.0 * ATR, rel=1e-6)


def test_breakeven_trigger_distance_is_side_symmetric():
    """The same probe depth mirrored across the two sides must reach the same
    verdict. This is the invariant the squared direction factor broke: a long
    needed a genuine 0.5 x ATR of profit to arm the breakeven while a short got
    it for free on the first bar."""
    verdicts = {}
    for side in (1, -1):
        bt = EnsembleBacktester(_zero_cost_cfg(breakeven_trigger_atr=0.5), asset_key="TEST")
        if side == 1:
            df = _df(n=10, price=PRICE)
            df.loc[2, "high"] = PRICE + 0.6 * ATR  # 0.6 ATR in favour
            df.loc[3, "low"] = PRICE - 2.1 * ATR  # then through the stop
        else:
            df = _short_df()
            df.loc[2, "low"] = PRICE - 0.6 * ATR
            df.loc[3, "high"] = PRICE + 2.1 * ATR
        trades = bt.run(df)
        assert len(trades) == 1
        assert trades[0].direction == side
        verdicts[side] = trades[0].exit_reason
    assert verdicts[1] == verdicts[-1] == "breakeven"
