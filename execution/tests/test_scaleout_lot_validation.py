import pytest

from execution.portfolio_allocator import validate_scaleout_tranches


def test_task7_scaleout_lot_validation_fails_on_small_lot():
    """Unit test Task 7: base lot 0.01 with 50/30/20 scheme produces fractional
    volumes (0.005, 0.003, 0.002) below 0.01 lot step, which fails validation."""
    is_valid, err_msg, tranches = validate_scaleout_tranches(
        base_volume=0.01,
        scaleout_ratios=[0.5, 0.3, 0.2],
        min_lot=0.01,
        lot_step=0.01,
        raise_on_invalid=False,
    )

    assert is_valid is False
    assert "Tranche 1 volume 0.0050 < min_lot" in err_msg or "volume" in err_msg

    # With raise_on_invalid=True it must raise ValueError
    with pytest.raises(ValueError, match="Invalid scale-out configuration"):
        validate_scaleout_tranches(
            base_volume=0.01,
            scaleout_ratios=[0.5, 0.3, 0.2],
            min_lot=0.01,
            lot_step=0.01,
            raise_on_invalid=True,
        )


def test_task7_scaleout_lot_validation_succeeds_on_0_10_lot():
    """Unit test Task 7: base lot 0.10 with 50/30/20 scheme produces exact
    valid tranches [0.05, 0.03, 0.02]."""
    is_valid, err_msg, tranches = validate_scaleout_tranches(
        base_volume=0.10,
        scaleout_ratios=[0.5, 0.3, 0.2],
        min_lot=0.01,
        lot_step=0.01,
        raise_on_invalid=True,
    )

    assert is_valid is True
    assert err_msg == ""
    assert tranches == [pytest.approx(0.05), pytest.approx(0.03), pytest.approx(0.02)]
