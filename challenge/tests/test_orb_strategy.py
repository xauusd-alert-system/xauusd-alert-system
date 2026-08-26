"""Tests for ORBStrategy — range, filters, breakout, gap direction, rotation."""
import pytest
from datetime import datetime, timedelta, timezone

from challenge.orb_strategy import ORBStrategy, ORBSignal

ET = timezone(timedelta(hours=-4))

CFG = {
    "strategy": {
        "tickers": ["TSLA", "AAPL", "NVDA"],
        "range_minutes": 15,
        "entry_end": "10:30",
        "all_positions_close": "15:30",
        "min_range_pct": 0.3,
        "max_range_pct": 1.5,
        "gap_skip_pct": 3.0,
        "min_volume_ratio": 1.5,
        "tp_r": 2.0,
    }
}


def _et(d, h, mi):
    return datetime(2026, d.month, d.day, h, mi)


class TestRangeCollection:
    def test_accumulates_range_candles(self):
        orb = ORBStrategy(CFG)
        now = _et(datetime(2026, 8, 25), 9, 35)
        candles_5m = {"TSLA": {"high": 250, "low": 248, "volume": 1000, "prev_close": 245}}
        orb.update(candles_5m, {}, now)
        st = orb._get_state("TSLA")
        assert st.range_high == 250
        assert st.range_low == 248
        assert st.range_candles == 1

    def test_range_extends_over_candles(self):
        orb = ORBStrategy(CFG)
        now1 = _et(datetime(2026, 8, 25), 9, 30)
        now2 = _et(datetime(2026, 8, 25), 9, 35)
        orb.update(
            {"TSLA": {"high": 250, "low": 248, "volume": 1000}}, {}, now1,
        )
        orb.update(
            {"TSLA": {"high": 252, "low": 247, "volume": 800}}, {}, now2,
        )
        st = orb._get_state("TSLA")
        assert st.range_high == 252
        assert st.range_low == 247


class TestFilters:
    def test_range_pct_filter_pass(self):
        orb = ORBStrategy(CFG)
        # Range 248-250 on mid 249 = 0.80% → within 0.3-1.5%
        assert orb._check_range_pct(250, 248) is True

    def test_range_pct_filter_too_tight(self):
        orb = ORBStrategy(CFG)
        # Range 249.5-250 = 0.20% → below 0.3%
        assert orb._check_range_pct(250, 249.5) is False

    def test_range_pct_filter_too_wide(self):
        orb = ORBStrategy(CFG)
        # Range 240-250 = ~4.08% → above 1.5%
        assert orb._check_range_pct(250, 240) is False

    def test_gap_filter_pass(self):
        orb = ORBStrategy(CFG)
        # Gap 250 vs prev 249 = 0.4% → below 3%
        assert orb._check_gap(250, 249) is True

    def test_gap_filter_skip(self):
        orb = ORBStrategy(CFG)
        # Gap 250 vs prev 240 = 4.17% → above 3%
        assert orb._check_gap(250, 240) is False

    def test_volume_filter_pass(self):
        orb = ORBStrategy(CFG)
        # 1500 volume, avg 1000 → 1.5x → passes (>=1.5)
        assert orb._check_volume(1500, 1000) is True

    def test_volume_filter_skip(self):
        orb = ORBStrategy(CFG)
        # 1000 volume, avg 1000 → 1.0x → below 1.5
        assert orb._check_volume(1000, 1000) is False

    def test_gap_direction_long_with_gap_up(self):
        orb = ORBStrategy(CFG)
        assert orb._check_gap_direction("long", 250, 245) is True

    def test_gap_direction_long_with_gap_down(self):
        orb = ORBStrategy(CFG)
        assert orb._check_gap_direction("long", 240, 245) is False

    def test_gap_direction_short_with_gap_down(self):
        orb = ORBStrategy(CFG)
        assert orb._check_gap_direction("short", 240, 245) is True

    def test_gap_direction_short_with_gap_up(self):
        orb = ORBStrategy(CFG)
        assert orb._check_gap_direction("short", 250, 245) is False

    def test_gap_direction_flat_allows_both(self):
        orb = ORBStrategy(CFG)
        assert orb._check_gap_direction("long", 245, 245) is True
        assert orb._check_gap_direction("short", 245, 245) is True

    def test_no_prev_close_allows(self):
        orb = ORBStrategy(CFG)
        assert orb._check_gap(250, 0) is True
        assert orb._check_gap_direction("long", 250, 0) is True


class TestBreakout:
    def test_long_breakout(self):
        orb = ORBStrategy(CFG)
        now_range = _et(datetime(2026, 8, 25), 9, 30)
        now_entry = _et(datetime(2026, 8, 25), 9, 50)
        # Accumulate range
        orb.update(
            {"TSLA": {"high": 250, "low": 248, "open": 249, "close": 249, "volume": 2000, "prev_close": 245}},
            {},
            now_range,
        )
        # Breakout above
        signals = orb.update(
            {},
            {"TSLA": {"high": 252, "low": 250.5, "close": 251, "volume": 500}},
            now_entry,
        )
        assert len(signals) == 1
        assert signals[0].bias == "long"
        assert signals[0].symbol == "TSLA"
        assert signals[0].entry > signals[0].range_high

    def test_short_breakout(self):
        orb = ORBStrategy(CFG)
        now_range = _et(datetime(2026, 8, 25), 9, 30)
        now_entry = _et(datetime(2026, 8, 25), 9, 50)
        # Range with gap down
        orb.update(
            {"TSLA": {"high": 250, "low": 248, "open": 247, "close": 249, "volume": 2000, "prev_close": 252}},
            {},
            now_range,
        )
        # Breakout below
        signals = orb.update(
            {},
            {"TSLA": {"high": 247.5, "low": 246, "close": 246.5, "volume": 500}},
            now_entry,
        )
        assert len(signals) == 1
        assert signals[0].bias == "short"

    def test_no_breakout_within_range(self):
        orb = ORBStrategy(CFG)
        now_range = _et(datetime(2026, 8, 25), 9, 30)
        now_entry = _et(datetime(2026, 8, 25), 9, 50)
        orb.update(
            {"TSLA": {"high": 250, "low": 248, "open": 249, "close": 249, "volume": 2000, "prev_close": 245}},
            {},
            now_range,
        )
        # Price stays within range
        signals = orb.update(
            {},
            {"TSLA": {"high": 249.5, "low": 248.5, "close": 249, "volume": 500}},
            now_entry,
        )
        assert len(signals) == 0

    def test_one_signal_per_symbol(self):
        orb = ORBStrategy(CFG)
        now_range = _et(datetime(2026, 8, 25), 9, 30)
        now_entry1 = _et(datetime(2026, 8, 25), 9, 50)
        now_entry2 = _et(datetime(2026, 8, 25), 10, 0)
        orb.update(
            {"TSLA": {"high": 250, "low": 248, "open": 249, "close": 249, "volume": 2000, "prev_close": 245}},
            {},
            now_range,
        )
        orb.update(
            {},
            {"TSLA": {"high": 252, "low": 250.5, "close": 251, "volume": 500}},
            now_entry1,
        )
        # Second signal attempt — should be blocked (already signaled)
        signals = orb.update(
            {},
            {"TSLA": {"high": 253, "low": 251, "close": 252, "volume": 500}},
            now_entry2,
        )
        assert len(signals) == 0


class TestSessionReset:
    def test_state_resets_on_new_day(self):
        orb = ORBStrategy(CFG)
        now1 = _et(datetime(2026, 8, 25), 9, 30)
        now2 = _et(datetime(2026, 8, 26), 9, 30)
        orb.update(
            {"TSLA": {"high": 250, "low": 248, "volume": 1000}},
            {},
            now1,
        )
        orb._get_state("TSLA").signaled = True
        # New day
        orb.update(
            {"TSLA": {"high": 300, "low": 298, "volume": 1000}},
            {},
            now2,
        )
        st = orb._get_state("TSLA")
        assert st.signaled is False
        assert st.range_high == 300


class TestPremarketRotation:
    def test_rank_by_premarket_volume(self):
        orb = ORBStrategy(CFG)
        ranked = orb.rank_by_premarket_volume({
            "TSLA": 50000,
            "AAPL": 100000,
            "NVDA": 30000,
        })
        assert ranked[0] == "AAPL"
        assert ranked[-1] == "NVDA"


class TestNoSignalsOutsideEntryWindow:
    def test_no_signals_in_range_phase(self):
        orb = ORBStrategy(CFG)
        now = _et(datetime(2026, 8, 25), 9, 35)  # range phase
        signals = orb.update(
            {},
            {"TSLA": {"high": 252, "low": 246, "close": 251, "volume": 500}},
            now,
        )
        assert len(signals) == 0

    def test_no_signals_after_entry_end(self):
        orb = ORBStrategy(CFG)
        now = _et(datetime(2026, 8, 25), 10, 35)  # after 10:30
        signals = orb.update(
            {},
            {"TSLA": {"high": 252, "low": 246, "close": 251, "volume": 500}},
            now,
        )
        assert len(signals) == 0


class TestORBSignalDataclass:
    def test_signal_attributes(self):
        sig = ORBSignal(
            symbol="TSLA",
            bias="long",
            entry=250.0,
            stop=248.0,
            tp=254.0,
            range_high=250.0,
            range_low=248.0,
            range_pct=0.8,
            volume_ratio=1.5,
            gap_pct=2.0,
        )
        assert sig.symbol == "TSLA"
        assert sig.bias == "long"
        assert sig.entry == 250.0
