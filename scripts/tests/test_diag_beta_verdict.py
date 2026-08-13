"""The market-beta verdict must be a function of |corr|, with the sign reported.

WHY
---
On 2026-08-14 the honest master run measured, on XAUUSD M15 over the 12
pre-lock folds:

    corr(fold PnL, buy-and-hold) = -0.653
    long share = 6.3% of 365 trades
    buy-and-hold over the same windows = +13900.8

and printed "fold results are not explained by market drift". That was the
exact opposite of the truth. The book is 93.7% short into a rising sample, and
the worst fold of the run (fold 7: -2293.4) is the fold where buy-and-hold made
the most money (+6493.7). The folds are explained by the drift; they are just
on the wrong side of it.

The old ladder tested `corr >= 0.6` and `corr >= 0.3`, so the entire negative
half of the line fell through to the "no beta" branch. These tests pin the
replacement: magnitude decides the bucket, sign decides the wording.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.diag_direction_beta import BETA_PARTIAL, BETA_STRONG, beta_verdict


def _text(corr: float) -> str:
    return " ".join(beta_verdict(corr)).lower()


def test_the_measured_negative_correlation_is_not_reported_as_absence_of_beta():
    """corr = -0.653 is the value that exposed the bug. It must read as beta."""
    text = _text(-0.653)
    assert "beta" in text
    assert "not explained by market drift" not in text
    assert "wrong side" in text


def test_a_strong_positive_correlation_is_still_reported_as_beta():
    text = _text(0.87)
    assert "beta" in text
    assert "not explained by market drift" not in text


def test_the_verdict_bucket_depends_on_magnitude_only():
    """Mirrored correlations land in the same bucket, with opposite wording."""
    for value in (BETA_STRONG + 0.05, BETA_PARTIAL + 0.05):
        positive = _text(value)
        negative = _text(-value)
        assert ("beta" in positive) == ("beta" in negative)
        assert ("partial market dependence" in positive) == (
            "partial market dependence" in negative)
        assert positive != negative, "the sign must still be visible in the wording"


def test_a_near_zero_correlation_clears_the_market_explanation():
    for value in (0.05, -0.05, 0.0):
        assert "not explained by market drift" in _text(value)


def test_an_undefined_correlation_does_not_clear_anything():
    """Too few traded folds means beta is unknown, not absent."""
    text = _text(float("nan"))
    assert "not explained by market drift" not in text
    assert "unavailable" in text
