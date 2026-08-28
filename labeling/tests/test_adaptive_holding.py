"""Tests for the adaptive holding period (Task 9).

Covers:

* unit behaviour of adaptive_holding_period (volatility thresholds, NaN/price
  guards, minimum horizon 1);
* REGRESSION: adaptive_holding=false must produce labels byte-for-byte
  identical to the pre-change behaviour (both fixed and atr_scaled methods,
  and the traded event);
* adaptive_holding=true must vary the horizon by volatility and differ from
  the fixed-horizon result on a frame where volatility changes;
* determinism: two runs produce identical label arrays.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from labeling.label_generator import (
    adaptive_holding_period,
    generate_labels,
    generate_labels_atr_scaled,
    generate_labels_atr_scaled_with_horizons,
    generate_labels_from_config,
    generate_labels_with_horizons,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_cfg(**labeling_overrides) -> dict:
    lab = {
        "event": "barrier",
        "method": "atr_scaled",
        "horizon_candles_n": 8,
        "target_atr_multiplier": 1.2,
        "stop_atr_multiplier": 1.0,
        "atr_column": "atr",
        "adaptive_holding": False,
        "adaptive_high_vol_pct": 0.02,
        "adaptive_mid_vol_pct": 0.01,
        "price_column": "close",
    }
    lab.update(labeling_overrides)
    return {"labeling": lab}


def _sawtooth_df(n: int = 120, atr: float = 1.0) -> pd.DataFrame:
    """Deterministic sawtooth prices with a constant ATR column.

    The sawtooth makes barrier touches happen at known bar offsets, so
    horizon changes visibly change the labels.
    """
    closes = []
    level = 100.0
    for i in range(n):
        closes.append(level)
        level = level + atr if (i % 6) < 3 else level - atr
    highs = [c + atr * 0.6 for c in closes]
    lows = [c - atr * 0.6 for c in closes]
    opens = [c for c in closes]
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "atr": [atr] * n,
        }
    )


# ---------------------------------------------------------------------------
# Unit: adaptive_holding_period thresholds
# ---------------------------------------------------------------------------


def test_unit_high_vol_quarter_horizon():
    # vol_pct = 3 / 100 = 0.03 > 0.02 -> base // 4
    assert adaptive_holding_period(3.0, 100.0, 36, 0.02, 0.01) == 9


def test_unit_mid_vol_half_horizon():
    # vol_pct = 0.015: above mid (0.01), not above high (0.02) -> base // 2
    assert adaptive_holding_period(1.5, 100.0, 36, 0.02, 0.01) == 18


def test_unit_low_vol_full_horizon():
    # vol_pct = 0.01: not strictly above mid -> full base
    assert adaptive_holding_period(1.0, 100.0, 36, 0.02, 0.01) == 36


def test_unit_thresholds_are_strictly_greater():
    # Exactly at the high threshold (0.02) -> only the mid branch applies
    # (0.02 is not > 0.02, but 0.02 > 0.01) -> base // 2, not base // 4.
    assert adaptive_holding_period(2.0, 100.0, 36, 0.02, 0.01) == 18


def test_unit_nan_atr_falls_back_to_base():
    assert adaptive_holding_period(float("nan"), 100.0, 36, 0.02, 0.01) == 36


def test_unit_nonpositive_atr_falls_back_to_base():
    assert adaptive_holding_period(0.0, 100.0, 36, 0.02, 0.01) == 36
    assert adaptive_holding_period(-1.0, 100.0, 36, 0.02, 0.01) == 36


def test_unit_nonpositive_price_falls_back_to_base():
    assert adaptive_holding_period(1.0, 0.0, 36, 0.02, 0.01) == 36
    assert adaptive_holding_period(1.0, -5.0, 36, 0.02, 0.01) == 36


def test_unit_minimum_horizon_is_one():
    # base // 4 and base // 2 must never collapse to 0.
    assert adaptive_holding_period(10.0, 100.0, 3, 0.02, 0.01) == 1
    assert adaptive_holding_period(1.5, 100.0, 1, 0.02, 0.01) == 1


# ---------------------------------------------------------------------------
# REGRESSION: adaptive_holding=false is byte-for-byte identical
# ---------------------------------------------------------------------------


def test_regression_atr_scaled_false_identical():
    df = _sawtooth_df()
    legacy = generate_labels_atr_scaled(df, 1.2, 1.0, 8, atr_col="atr")
    via_config = generate_labels_from_config(df, _base_cfg())
    np.testing.assert_array_equal(legacy.values, via_config.values)


def test_regression_fixed_false_identical():
    df = _sawtooth_df()
    # target/stop in absolute price units for the fixed method.
    cfg = _base_cfg(method="fixed", target_pips_x=1.2, stop_pips_y=1.0)
    legacy = generate_labels(df, 1.2, 1.0, 8)
    via_config = generate_labels_from_config(df, cfg)
    np.testing.assert_array_equal(legacy.values, via_config.values)


def test_regression_traded_false_identical():
    """With adaptive_holding=false the traded event must not receive per-bar
    horizons at all (horizons=None keeps the exact legacy scan).

    generate_labels_from_config emits the traded label in the "pm1" encoding
    (see its docstring), so the legacy reference here is the direction
    labeller, not the per-side event labeller.
    """
    from labeling.label_generator import generate_labels_traded_direction

    df = _sawtooth_df()
    cfg = {
        "labeling": {"horizon_candles_n": 8, "atr_column": "atr"},
        "backtest": {},
        "assets": {"XAUUSD": {}},
    }
    legacy = generate_labels_traded_direction(df, cfg, "XAUUSD", encoding="pm1")
    via_config = generate_labels_from_config(
        df,
        {
            **cfg,
            "labeling": {**cfg["labeling"], "event": "traded", "adaptive_holding": False},
        },
        asset_key="XAUUSD",
    )
    np.testing.assert_array_equal(legacy.values, via_config.values)


# ---------------------------------------------------------------------------
# adaptive_holding=true: horizons actually change
# ---------------------------------------------------------------------------


def _df_with_varying_vol(n: int = 90) -> pd.DataFrame:
    """Sawtooth frame whose ATR (and thus vol_pct) switches by third:
    calm (vol 0.5%) -> mid (1.5%) -> high (3%)."""
    df = _sawtooth_df(n=n)
    thirds = n // 3
    atrs = [0.5] * thirds + [1.5] * thirds + [3.0] * (n - 2 * thirds)
    df["atr"] = atrs
    # Scale OHLC amplitude with ATR so touches stay realistic.
    df["high"] = df["close"] + df["atr"] * 0.6
    df["low"] = df["close"] - df["atr"] * 0.6
    return df


def test_adaptive_horizons_vary_by_volatility():
    df = _df_with_varying_vol()
    from labeling.label_generator import _adaptive_horizons

    horizons = _adaptive_horizons(df, _base_cfg()["labeling"], base_period=8)
    thirds = len(df) // 3
    # calm (0.5% <= 1%): full horizon
    assert all(h == 8 for h in horizons[:thirds])
    # mid (1.5% > 1%): half horizon
    assert all(h == 4 for h in horizons[thirds : 2 * thirds])
    # high (3% > 2%): quarter horizon
    assert all(h == 2 for h in horizons[2 * thirds :])


def test_adaptive_differs_from_fixed_on_mixed_vol_frame():
    df = _df_with_varying_vol()
    fixed = generate_labels_from_config(df, _base_cfg())
    adaptive = generate_labels_from_config(df, _base_cfg(adaptive_holding=True))
    fixed_vals = fixed.values
    adaptive_vals = adaptive.values
    assert fixed_vals.shape == adaptive_vals.shape
    # At least one resolved label differs (the mid/high-vol segments scan
    # fewer bars), and both produce at least one resolved label.
    assert (~np.isnan(fixed_vals)).any()
    assert not np.array_equal(fixed_vals, adaptive_vals, equal_nan=True)


def test_adaptive_identical_when_all_bars_calm():
    """A constant-calm frame gets the full horizon everywhere, so adaptive
    must equal fixed exactly (equal_nan)."""
    df = _sawtooth_df()  # constant atr=1.0 on price 100 -> vol 1%: not > mid
    fixed = generate_labels_from_config(df, _base_cfg())
    adaptive = generate_labels_from_config(df, _base_cfg(adaptive_holding=True))
    np.testing.assert_array_equal(fixed.values, adaptive.values)


def test_per_bar_horizon_scanner_matches_legacy_for_constant_horizon():
    """generate_labels_with_horizons with a constant horizon array must equal
    the legacy fixed-horizon labeller — the per-bar scanner adds no bias."""
    df = _sawtooth_df()
    legacy = generate_labels(df, 1.2, 1.0, 8)
    horizons = np.full(len(df), 8, dtype=int)
    per_bar = generate_labels_with_horizons(df, 1.2, 1.0, horizons)
    np.testing.assert_array_equal(legacy.values, per_bar.values)

    legacy_atr = generate_labels_atr_scaled(df, 1.2, 1.0, 8, atr_col="atr")
    per_bar_atr = generate_labels_atr_scaled_with_horizons(df, 1.2, 1.0, horizons, atr_col="atr")
    np.testing.assert_array_equal(legacy_atr.values, per_bar_atr.values)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_adaptive_deterministic_two_runs():
    df = _df_with_varying_vol()
    cfg = _base_cfg(adaptive_holding=True)
    first = generate_labels_from_config(df, cfg).values
    second = generate_labels_from_config(df, cfg).values
    np.testing.assert_array_equal(first, second)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_adaptive_requires_atr_column():
    df = _sawtooth_df().drop(columns=["atr"])
    with pytest.raises(ValueError, match="ATR column"):
        generate_labels_from_config(df, _base_cfg(adaptive_holding=True))
