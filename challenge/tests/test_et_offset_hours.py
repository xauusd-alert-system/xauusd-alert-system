"""Unit tests for the DST-aware Eastern offset used by the ORB strategy.

``ORBStrategy.et_offset_hours`` (and the identical static helper on
``SessionSimulator``) decide US Eastern offset: -4 (EDT) during DST, -5 (EST)
otherwise. DST runs from the 2nd Sunday of March (02:00 local) to the 1st
Sunday of November (02:00 local).

Important implementation detail: the function operates at DATE granularity and
uses ``dst_start <= d < dst_end``, so:
  * a date ON the 2nd Sunday of March is already EDT  (-4),
  * a date ON the 1st Sunday of November is EST (-5), never EDT.
This matches the practical need (session windows span whole days) but is
slightly looser than the real 02:00 transition; these tests pin the exact
behaviour so it stays stable.

The tests cover 2026 and 2027 (non-leap/leap weekday shifts) plus the usual
cross-boundary controls to guard against off-by-one regressions.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from challenge.orb_strategy import ORBStrategy
from challenge.stealth.session_simulator import SessionSimulator

# Both implementations compute the same dates; parametrize over the two classes
# so nothing drifts apart silently.
_IMPLS = [
    pytest.param(ORBStrategy.et_offset_hours, id="orb_strategy"),
    pytest.param(SessionSimulator.et_offset_hours, id="session_simulator"),
]

# (transition_boundary_date_inclusive, expected_offset)
# Day before / on / after each transition, per year.
_EDT = -4
_EST = -5


# ---------------------------------------------------------------------------
# 2026 — DST: Sun Mar 8 -> Sun Nov 1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("impl", _IMPLS)
@pytest.mark.parametrize("when,expected", [
    (date(2026, 1, 1), _EST),      # deep winter, EST
    (date(2026, 3, 7), _EST),      # day before DST start
    (date(2026, 3, 8), _EDT),      # 2nd Sunday of March -> EDT (inclusive)
    (date(2026, 3, 9), _EDT),      # day after DST start
    (date(2026, 6, 21), _EDT),     # summer solstice, EDT
    (date(2026, 10, 31), _EDT),    # day before DST end
    (date(2026, 11, 1), _EST),     # 1st Sunday of Nov -> EST (exclusive of DST)
    (date(2026, 11, 2), _EST),     # day after DST end
    (date(2026, 12, 31), _EST),    # deep winter, EST
])
def test_et_offset_2026_boundaries(impl, when, expected):
    assert impl(when) == expected


# ---------------------------------------------------------------------------
# 2027 — DST: Sun Mar 14 -> Sun Nov 7 (weekday shifted, NOT a leap year)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("impl", _IMPLS)
@pytest.mark.parametrize("when,expected", [
    (date(2027, 1, 1), _EST),
    (date(2027, 3, 13), _EST),     # day before 2nd Sunday of March
    (date(2027, 3, 14), _EDT),     # 2nd Sunday of March 2027 = Mar 14
    (date(2027, 3, 15), _EDT),
    (date(2027, 7, 4), _EDT),      # Independence Day, EDT
    (date(2027, 11, 6), _EDT),     # day before 1st Sunday of Nov
    (date(2027, 11, 7), _EST),     # 1st Sunday of Nov 2027 = Nov 7
    (date(2027, 11, 8), _EST),
    (date(2027, 12, 31), _EST),
])
def test_et_offset_2027_boundaries(impl, when, expected):
    assert impl(when) == expected


# ---------------------------------------------------------------------------
# Boundary date derivation sanity (independent recomputation of DST dates)
# ---------------------------------------------------------------------------

def _second_sunday(y: int, month: int) -> date:
    first = date(y, month, 1)
    first_sun = first.replace(day=1 + (6 - first.weekday()) % 7)
    return first_sun + timedelta(days=7)


def _first_sunday(y: int, month: int) -> date:
    first = date(y, month, 1)
    return first.replace(day=1 + (6 - first.weekday()) % 7)


@pytest.mark.parametrize("year,start,stop", [
    (2026, date(2026, 3, 8), date(2026, 11, 1)),
    (2027, date(2027, 3, 14), date(2027, 11, 7)),
])
def test_et_offset_dst_dates_match_reference_rules(year, start, stop):
    """Verify the hardcoded years against the documented rule recomputed
    independently (2nd Sunday of March / 1st Sunday of November)."""
    assert _second_sunday(year, 3) == start
    assert _first_sunday(year, 11) == stop
    # Inclusive start, exclusive stop (the <= / < used in the implementation).
    assert ORBStrategy.et_offset_hours(start) == _EDT
    assert ORBStrategy.et_offset_hours(stop) == _EST


# ---------------------------------------------------------------------------
# Consistency between the two implementations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("when", [
    date(2026, 1, 1), date(2026, 3, 8), date(2026, 11, 1),
    date(2027, 3, 14), date(2027, 11, 7), date(2027, 12, 31),
    date(2028, 3, 12), date(2028, 11, 5),  # 2028 is a LEAP year — weekday shift again
])
def test_both_implementations_agree(when):
    assert ORBStrategy.et_offset_hours(when) == SessionSimulator.et_offset_hours(when)


# ---------------------------------------------------------------------------
# Leap-year sanity (2028) — the weekday of Mar 1 shifts by an extra day
# ---------------------------------------------------------------------------

def test_leap_year_2028_boundaries():
    # 2028 is a leap year; Mar 1, 2028 is a Wednesday.
    assert ORBStrategy.et_offset_hours(date(2028, 3, 11)) == _EST  # Sat before
    assert ORBStrategy.et_offset_hours(date(2028, 3, 12)) == _EDT  # 2nd Sun Mar 2028
    assert ORBStrategy.et_offset_hours(date(2028, 11, 4)) == _EDT  # Sat before
    assert ORBStrategy.et_offset_hours(date(2028, 11, 5)) == _EST  # 1st Sun Nov 2028