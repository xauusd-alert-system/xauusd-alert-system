# -*- coding: utf-8 -*-
"""Stage D: UTEX provider on RECORDED payloads (zero network) + ranker +
NY session math + signal-only runner without any executor in the graph."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from usstocks.data.utex_provider import UtexClient, decode_candles
from usstocks.models import Bar, PremarketSnapshot, RiskState
from usstocks.premarket_ranker import (
    ScannerConfig,
    build_snapshot,
    build_watchlist,
    passes_filters,
    score_snapshot,
)
from usstocks.scanner_loop import SignalOnlyRunner
from usstocks.session import NySession, parse_holidays
from tests.fixtures.vwap_scenarios import benchmark_uptrend, long_scenario


# ---------------------------------------------------------------------------
# UTEX provider (recorded fixture transport)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)[:200]

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


RECORDED_CANDLES = {
    # UTEX integer prices are scaled by 1e8; floats pass through as-is.
    "candles": [
        {"time": 1787600100, "open": 10025000000, "high": 10090000000,
         "low": 10010000000, "close": 10075000000, "volume": 12345},
        {"time": 1787600160, "open": 10.05, "high": 10.09,
         "low": 10.01, "close": 10.07, "volume": 5},
    ]
}


def test_decode_candles_scales_integers_and_sorts():
    out = decode_candles(RECORDED_CANDLES)
    assert [d["time"] for d in out] == sorted(d["time"] for d in out)
    # the earlier candle carries 1e8-scaled integers -> ~100.25
    assert out[0]["open"] == pytest.approx(100.25)
    # the later one already uses floats
    assert out[1]["close"] == pytest.approx(10.07)


def test_client_fetch_bars_offline():
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["payload"] = kw.get("json")
        return FakeResponse(RECORDED_CANDLES)

    client = UtexClient(post=fake_post, token_file="unused")
    bars = client.fetch_bars("token", "SID_AMD", candles_count=2)
    assert len(bars) == 2 and isinstance(bars[0], Bar)
    assert bars[0].ts.tzinfo is not None          # aware UTC
    assert "getCandlesToDate" in captured["url"]
    assert captured["payload"]["interval"] == "Min1"
    assert captured["payload"]["candlesCount"] == 2


def test_client_non_network_error_propagates_without_fallback():
    def boom(url, **kw):
        raise RuntimeError("auth rejected")       # not a network error

    client = UtexClient(post=boom, token_file="unused")
    with pytest.raises(RuntimeError, match="auth rejected"):
        client.fetch_candle_dicts("t", "S", 5)


# ---------------------------------------------------------------------------
# Premarket ranker (ТЗ §7.1)
# ---------------------------------------------------------------------------

def _snap(**kw) -> PremarketSnapshot:
    base = dict(symbol="AMD", price=150.0, prev_close=147.0, gap_pct=2.04,
                relative_volume=2.1, avg_daily_dollar_volume=80e6,
                spread_pct=0.05, fresh_news_catalyst=True)
    base.update(kw)
    return PremarketSnapshot(**base)


CFG = ScannerConfig()


def test_scoring_formula_matches_tz():
    s = score_snapshot(_snap())                       # gap+2 rv+3 news+2 adv+1 px/spread+2
    assert s == 10
    # gap1.6:0, rv1.6:+1, news:0, adv:+1, px/spread:+2 => 4
    assert score_snapshot(_snap(gap_pct=1.6, relative_volume=1.6,
                                fresh_news_catalyst=False)) == 4
    assert score_snapshot(_snap(price=9.0, spread_pct=0.05)) <= 8      # no price/spread pts


def test_filters_block_each_dimension():
    ok, why = passes_filters(_snap(), CFG)
    assert ok
    assert not passes_filters(_snap(price=9.99), CFG)[0]
    assert not passes_filters(_snap(gap_pct=1.4), CFG)[0]
    assert not passes_filters(_snap(relative_volume=1.49), CFG)[0]
    assert not passes_filters(_snap(avg_daily_dollar_volume=49e6), CFG)[0]


def test_watchlist_is_top3_by_score():
    snaps = [_snap(symbol="A"), _snap(symbol="B"),
             _snap(symbol="C", relative_volume=1.6),
             _snap(symbol="D", relative_volume=1.55)]
    items = build_watchlist(snaps, CFG)
    assert len(items) == 3
    scores = [it.snapshot.score for it in items]
    assert scores == sorted(scores, reverse=True)


def test_build_snapshot_from_bars_math():
    from usstocks.models import Bar as B
    day1 = long_scenario()
    day2 = [B(ts=b.ts + timedelta(days=1), open=b.open, high=b.high,
              low=b.low, close=b.close, volume=b.volume) for b in day1]
    snap = build_snapshot("AMD", day1 + day2)
    assert snap is not None
    assert snap.prev_close == pytest.approx(day1[-1].close)
    expected_gap = (day2[0].open - day1[-1].close) / day1[-1].close * 100
    assert snap.gap_pct == pytest.approx(expected_gap, abs=0.01)
    # identical volume profiles -> relative volume ~1.0
    assert snap.relative_volume == pytest.approx(1.0, abs=0.02)
    assert snap.avg_daily_dollar_volume > 0


# ---------------------------------------------------------------------------
# NY session / DST
# ---------------------------------------------------------------------------

HOLIDAYS = parse_holidays(["2026-07-03"])


def test_dst_offsets_change_across_transitions():
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
    winter = datetime(2026, 12, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
    from usstocks.session import ensure_ny
    assert ensure_ny(summer).utcoffset() == timedelta(hours=-4)
    assert ensure_ny(winter).utcoffset() == timedelta(hours=-5)


def test_session_open_close_are_wall_clock_ny():
    s = NySession(holidays=HOLIDAYS)
    o = s.session_open(date(2026, 7, 15))
    c = s.session_close(date(2026, 7, 15))
    assert (o.hour, o.minute) == (9, 30) and (c.hour, c.minute) == (16, 0)


def test_weekend_and_holiday_detection():
    s = NySession(holidays=HOLIDAYS)
    assert not s.is_trading_day(date(2026, 7, 4))     # Saturday
    assert not s.is_trading_day(date(2026, 7, 3))     # observed holiday
    assert s.is_trading_day(date(2026, 7, 6))         # Monday


def test_minutes_to_close_positive_before_close():
    s = NySession()
    now = datetime(2026, 7, 15, 15, 30, tzinfo=ZoneInfo("America/New_York"))
    assert s.minutes_to_close(now) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Signal-only runner: signals flow to notifier, NEVER to an executor.
# ---------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, bars_by_symbol):
        self.bars = {k.upper(): v for k, v in bars_by_symbol.items()}
        self.calls = []

    def get_bars(self, symbol, count):
        self.calls.append((symbol.upper(), count))
        if symbol.upper() in ("QQQ", "SPY"):
            return benchmark_uptrend(count // 5)
        return self.bars[symbol.upper()]


class SpyExecutor:
    """ТЗ §12.14: must never be called in the us_stocks pipeline."""

    def __init__(self):
        self.submitted = []

    def submit(self, order):
        self.submitted.append(order)
        raise AssertionError("executor.submit called in signal-only profile")


class CaptureNotifier:
    def __init__(self):
        self.signals, self.risk_events, self.watchlists = [], [], []

    def send_signal(self, s):
        self.signals.append(s)

    def send_risk_event(self, e):
        self.risk_events.append(e)

    def send_watchlist(self, w):
        self.watchlists.append(w)


BASE_CFG = {
    "risk": {"risk_per_trade_usd": 10.0, "personal_daily_stop_usd": -20.0,
             "max_trades_per_day": 2, "max_consecutive_losses": 2,
             "daily_profit_lock_usd": 20.0,
             "no_new_entries_minutes_before_close": 25},
    "challenge": {"max_notional_usd": 5000.0},
    "strategy": {},
    "us_stocks": {"tech_symbols": ["AMD"]},
    "session": {"holidays": []},
}
NOW = datetime(2026, 8, 26, 10, 40, tzinfo=ZoneInfo("America/New_York"))


def _runner(provider, notifier, state=None):
    return SignalOnlyRunner(
        BASE_CFG, provider, notifier, watchlist=["AMD"],
        state=state or RiskState(session_date="2026-08-26"),
        symbol_ids={"AMD": "SID", "QQQ": "SID_Q"})


def test_runner_delivers_signal_without_any_executor_call():
    spy = SpyExecutor()
    notifier = CaptureNotifier()
    runner = _runner(FakeProvider({"AMD": long_scenario()}), notifier)
    runner.scan_once(NOW)
    assert len(notifier.signals) == 1                  # signal delivered...
    assert spy.submitted == []                         # ...and nothing executed
    assert notifier.signals[0].shares > 0
    assert runner.state.active_symbol == "AMD"         # single-active marked


def test_runner_respects_risk_block_and_reports_event():
    notifier = CaptureNotifier()
    blocked = RiskState(session_date="2026-08-26",
                        realized_pnl_usd=-25.0)        # past personal stop
    runner = _runner(FakeProvider({"AMD": long_scenario()}), notifier, blocked)
    sigs = runner.scan_once(NOW)
    assert sigs == [] and notifier.signals == []
    assert any(e.code == "PERSONAL_DAILY_STOP" for e in notifier.risk_events)


def test_runner_flat_data_no_signals_no_crash():
    from tests.fixtures.vwap_scenarios import flat_scenario
    notifier = CaptureNotifier()
    runner = _runner(FakeProvider({"AMD": flat_scenario()}), notifier)
    assert runner.scan_once(NOW) == []
