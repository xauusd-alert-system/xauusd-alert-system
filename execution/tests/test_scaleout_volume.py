"""Tests for the audit W2 scale-out volume fix in execution/mt5_trader.py.

A 50/30/20 scale-out on a 0.01 base lot used to round(0.005, 2) -> 0.01
(closing the WHOLE position as "TP1 (50%)") and round(0.003, 2) -> 0.0 (a
zero-volume order). The fix quantizes each tranche to the broker's
volume_step/volume_min and returns 0 (skip) when the tranche is not fillable.
"""
import pytest

from execution.mt5_trader import MultiAssetMT5Trader


class _Info:
    def __init__(self, volume_step=0.01, volume_min=0.01):
        self.volume_step = volume_step
        self.volume_min = volume_min


@pytest.fixture()
def trader():
    # Construct without __init__ (which loads config + pipelines); only the
    # pure `_scaleout_volume` helper is exercised.
    return object.__new__(MultiAssetMT5Trader)


def test_tiny_base_lot_tranches_are_skipped(trader):
    """0.01 base lot -> 50% = 0.005 and 30% = 0.003 are below min -> skipped."""
    info = _Info()
    assert trader._scaleout_volume("XAUUSD", info, 0.01, 0.5) == 0.0
    assert trader._scaleout_volume("XAUUSD", info, 0.01, 0.3) == 0.0


def test_fillable_tranches_are_quantized(trader):
    """0.10 base lot -> 0.05 / 0.03 / 0.02 are exact multiples of the step."""
    info = _Info()
    assert trader._scaleout_volume("XAUUSD", info, 0.10, 0.5) == pytest.approx(0.05)
    assert trader._scaleout_volume("XAUUSD", info, 0.10, 0.3) == pytest.approx(0.03)
    assert trader._scaleout_volume("XAUUSD", info, 0.10, 0.2) == pytest.approx(0.02)


def test_rounds_down_to_lot_step(trader):
    """A non-multiple tranche is rounded DOWN to the step, never up past it."""
    info = _Info()
    # 0.07 * 0.5 = 0.035 -> floor to 0.03 (fillable, not 0.04)
    assert trader._scaleout_volume("XAUUSD", info, 0.07, 0.5) == pytest.approx(0.03)
