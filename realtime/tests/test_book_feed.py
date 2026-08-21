"""
Unit tests for realtime/book_feed.py (Phase 0 collection + Phase 1 gate math).
Run with: pytest realtime/tests/test_book_feed.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from realtime.book_feed import (
    BAR_SECONDS,
    BookFeed,
    bar_ts_of,
    book_features_from_levels,
)
from model.ensemble import EnsembleSignal


# ---------------------------------------------------------------------------
# Per-snapshot feature math
# ---------------------------------------------------------------------------

def test_book_features_imbalance_signs():
    """+1.0 = ask-heavy; balanced book must give ~0.0; ask-heavy must be positive."""
    balanced = book_features_from_levels(
        [(100.0, 10), (99.5, 5)],
        [(100.5, 10), (101.0, 5)],
    )
    assert abs(balanced["imb1"]) < 1e-9
    assert abs(balanced["imb3"]) < 1e-9

    ask_heavy = book_features_from_levels(
        [(100.0, 2), (99.5, 1)],
        [(100.5, 18), (101.0, 9)],
    )
    assert ask_heavy["imb1"] > 0.5
    assert ask_heavy["imb3"] > 0.5

    bid_heavy = book_features_from_levels(
        [(100.0, 18), (99.5, 9)],
        [(100.5, 2), (101.0, 1)],
    )
    assert bid_heavy["imb1"] < -0.5
    assert bid_heavy["imb3"] < -0.5


def test_book_features_depth_ratio_and_walls():
    feats = book_features_from_levels(
        [(100.0, 40), (99.5, 1), (99.0, 1)],
        [(100.5, 4), (101.0, 4), (101.5, 4)],
    )
    assert feats["depth_ratio"] == pytest.approx(42.0 / 12.0)
    # walls: levels >= 10x median volume (median of [40,1,1,4,4,4] -> 4) => 40 counts
    assert feats["walls"] == 1


def test_book_features_empty_side_returns_none():
    assert book_features_from_levels([], [(100.5, 4)]) is None
    assert book_features_from_levels([(100.0, 4)], []) is None


def test_book_features_level_counts_and_microprice():
    feats = book_features_from_levels(
        [(100.0, 1), (99.5, 1)],
        [(100.5, 1), (101.0, 1)],
    )
    assert feats["bid_levels"] == 2
    assert feats["ask_levels"] == 2
    assert feats["microprice"] == pytest.approx(100.25)
    assert feats["spread"] == pytest.approx(0.5)


def test_bar_ts_of():
    assert bar_ts_of(1000, BAR_SECONDS) == 900
    assert bar_ts_of(900, BAR_SECONDS) == 900
    assert bar_ts_of(1199, BAR_SECONDS) == 900


# ---------------------------------------------------------------------------
# BookFeed aggregation + causality (fake MT5, no threads)
# ---------------------------------------------------------------------------

class FakeLevel:
    def __init__(self, price, volume):
        self.price = price
        self.volume = volume


class FakeTick:
    bid = 100.0
    ask = 100.5


class FakeMT5:
    def __init__(self):
        self.added = []
        self.removed = []
        self.levels = [
            FakeLevel(102.0, 100), FakeLevel(101.5, 5), FakeLevel(101.0, 5),   # asks
            FakeLevel(100.5, 5), FakeLevel(100.0, 5),                          # asks (<= 100.25 -> bid)
        ]

    def market_book_add(self, symbol):
        self.added.append(symbol)
        return True

    def market_book_remove(self, symbol):
        self.removed.append(symbol)
        return True

    def market_book_get(self, symbol):
        return tuple(self.levels)

    def symbol_info_tick(self, symbol):
        return FakeTick()


def _feed_with_fake_mt5(**kwargs) -> BookFeed:
    persist = kwargs.pop("persist", False)
    out_dir = kwargs.pop("out_dir", None)
    feed = BookFeed(
        {"assets": {"BTCUSD": {"mt5_symbol": "BITCOIN", "enabled": True}},
         "book_gate": {"assets": {"BTCUSD": {"enabled": True}}}},
        persist=persist,
        out_dir=out_dir,
        **kwargs,
    )
    feed._mt5 = FakeMT5()
    return feed


def test_book_feed_bar_features_finalizes_on_demand():
    feed = _feed_with_fake_mt5()
    feats = feed._snapshot_features("BITCOIN")
    assert feats is not None and feats["bid_levels"] > 0 and feats["ask_levels"] > 0
    # simulate snapshots inside bar 900
    for _ in range(4):
        feed._accumulate("BTCUSD", 900, feats)
    assert feed.bar_features("BTCUSD", 900) is not None
    assert feed.bar_features("BTCUSD", 900)["snapshots"] == 4


def test_book_feed_bar_rollover_finalizes_previous_bar():
    feed = _feed_with_fake_mt5()
    feats = feed._snapshot_features("BITCOIN")
    feed._accumulate("BTCUSD", 900, feats)
    feed._accumulate("BTCUSD", 1200, feats)  # rollover -> finalizes 900
    assert feed.bar_features("BTCUSD", 900)["snapshots"] == 1
    assert feed.bar_features("BTCUSD", 1200)["snapshots"] == 1


def test_book_feed_missing_bar_returns_none():
    feed = _feed_with_fake_mt5()
    feats = feed._snapshot_features("BITCOIN")
    feed._accumulate("BTCUSD", 900, feats)
    assert feed.bar_features("BTCUSD", 1500) is None


def test_book_feed_csv_persistence(tmp_path):
    feed = _feed_with_fake_mt5(out_dir=str(tmp_path), persist=True)
    feats = feed._snapshot_features("BITCOIN")
    feed._accumulate("BTCUSD", 900, feats)
    feed._finalize("BTCUSD", feed._current["BTCUSD"])
    path = os.path.join(str(tmp_path), "BITCOIN.csv")
    assert os.path.exists(path)
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert lines[0].startswith("bar_utc,snapshots")
    assert len(lines) == 2


def test_book_feed_overview_status():
    feed = _feed_with_fake_mt5()
    ov = feed.overview()
    assert ov["BTCUSD"]["configured"] is True
    assert ov["BTCUSD"]["subscribed"] is False  # subscription happens in _subscribe_all
    feed._subscribe_all()
    ov = feed.overview()
    assert ov["BTCUSD"]["subscribed"] is True


# ---------------------------------------------------------------------------
# Pipeline gate math (fail-open contract)
# ---------------------------------------------------------------------------

def _ensemble(bias="long", confidence=0.7) -> EnsembleSignal:
    return EnsembleSignal(
        bias=bias,
        confidence=confidence,
        rule_vote=1,
        ml_p_long=0.6,
        ml_p_short=0.4,
        regime="trend_up",
        suppressed_by_meta_filter=False,
        reasoning_summary="test",
    )


def _pipe_with_book_gate(bg: dict):
    """RealtimePipeline built on the real config with an overridden book_gate."""
    from config.loader import load_config
    from realtime.pipeline import RealtimePipeline
    cfg = load_config()
    cfg["book_gate"] = bg
    return RealtimePipeline(cfg=cfg, model_path=None, asset_key="BTCUSD", data_mode="mock")


def test_gate_vetoes_long_when_book_ask_heavy():
    pipe = _pipe_with_book_gate({"enabled": True, "veto_imbalance": 0.35})
    sig = _ensemble(bias="long", confidence=0.8)
    out = pipe._apply_book_gate(sig, {"imb5_last": 0.6})
    assert out["decision"] == "veto"
    assert sig.bias == "no_trade"
    assert sig.confidence == 0.0


def test_gate_vetoes_short_when_book_bid_heavy():
    pipe = _pipe_with_book_gate({"enabled": True, "veto_imbalance": 0.35})
    sig = _ensemble(bias="short", confidence=0.8)
    out = pipe._apply_book_gate(sig, {"imb5_last": -0.6})
    assert out["decision"] == "veto"
    assert sig.bias == "no_trade"


def test_gate_boosts_when_book_agrees():
    pipe = _pipe_with_book_gate({"enabled": True, "boost_confidence": 0.05})
    sig = _ensemble(bias="long", confidence=0.8)
    out = pipe._apply_book_gate(sig, {"imb5_last": -0.2})
    assert out["decision"] == "boost"
    assert sig.confidence == pytest.approx(0.85)


def test_gate_boost_capped_at_095():
    pipe = _pipe_with_book_gate({"enabled": True, "boost_confidence": 0.05})
    sig = _ensemble(bias="short", confidence=0.95)
    out = pipe._apply_book_gate(sig, {"imb5_last": 0.1})
    assert out["decision"] == "boost"
    assert sig.confidence == pytest.approx(0.95)


def test_gate_disabled_passes_through():
    pipe = _pipe_with_book_gate({"enabled": False})
    sig = _ensemble(bias="long", confidence=0.8)
    out = pipe._apply_book_gate(sig, {"imb5_last": 0.9})
    assert out["decision"] == "disabled"
    assert sig.bias == "long"
    assert sig.confidence == pytest.approx(0.8)