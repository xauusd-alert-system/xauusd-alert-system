# -*- coding: utf-8 -*-
"""ТЗ §12.2-3: VWAP math and Opening Range 15m on fixture OHLCV."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from usstocks.indicators import (
    NY,
    aggregate_to_5m,
    average_volume,
    drop_unclosed_1m,
    opening_range,
    session_vwap_series,
    volume_ratio,
)

from tests.fixtures.vwap_scenarios import long_scenario


def test_vwap_math_on_simple_series():
    b = lambda ts, o, h, l, c, v: type("B", (), {})  # noqa: E731 (replaced below)
    from datetime import timedelta
    from usstocks.models import Bar
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    bars = [
        Bar(t0, 10, 12, 9, 11, 300),                 # tp = 32/3 ≈ 10.667
        Bar(t0 + timedelta(minutes=5), 11, 13, 10, 12, 100),  # tp ≈ 11.667
        Bar(t0 + timedelta(minutes=10), 12, 14, 11, 13, 100),  # tp ≈ 12.333
    ]
    vw = session_vwap_series(bars)
    exp1 = (10 + 12) / 2                              # equal volumes
    assert abs(vw[1] - (10.6667 * 300 + 11.6667 * 100) / 400) < 1e-3
    assert vw[0] < vw[1] < vw[2]                      # rising series -> vwap lags below


def test_vwap_volume_weighting_dominates_heavy_bar():
    from datetime import timedelta
    from usstocks.models import Bar
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    light = Bar(t0, 100, 100, 100, 100, 1)
    heavy = Bar(t0 + timedelta(minutes=5), 110, 110, 110, 110, 999_999)
    vw = session_vwap_series([light, heavy])
    # (100*1 + 110*999999) / 1_000_000 = 109.99999 -> essentially the heavy price
    assert abs(vw[1] - 109.99999) < 1e-4


def test_aggregation_uses_only_closed_buckets():
    from datetime import timedelta
    bars = long_scenario()
    asof = bars[-1].ts                       # last bar's own minute not closed yet
    kept = drop_unclosed_1m(bars, asof)
    assert len(kept) == len(bars) - 1
    agg = aggregate_to_5m(kept)
    for a in agg:
        window = [x for x in kept if a.ts <= x.ts < a.ts + timedelta(minutes=5)]
        assert a.high == pytest.approx(max(x.high for x in window))
        assert a.low == pytest.approx(min(x.low for x in window))
        assert a.volume == pytest.approx(sum(x.volume for x in window))
    # partial bucket must be dropped entirely
    assert len(agg) * 5 <= len(kept)


def test_opening_range_needs_three_full_bars():
    bars = aggregate_to_5m(long_scenario())
    rng3 = opening_range(bars[:3])
    assert rng3 is not None and rng3[0] > rng3[1]
    assert opening_range(bars[:2]) is None   # range incomplete -> None, never partial


def test_opening_range_values_match_first_three_candles():
    bars = aggregate_to_5m(long_scenario())
    hi, lo = opening_range(bars[:3])
    assert hi == max(b.high for b in bars[:3])
    assert lo == min(b.low for b in bars[:3])


def test_volume_ratio_helper():
    from usstocks.models import Bar
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    hist = [Bar(t0.replace(minute=i), 1, 1, 1, 1, 100) for i in range(20)]
    assert average_volume(hist, 20) == 100
    probe = Bar(t0.replace(minute=20), 1, 1, 1, 1, 250)
    assert volume_ratio(probe, average_volume(hist, 20)) == 2.5
