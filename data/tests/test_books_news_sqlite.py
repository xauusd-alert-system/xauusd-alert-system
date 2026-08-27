"""TZ_BOOKS T-15: the SQLite news table (backtest-side news blackout)."""
from __future__ import annotations

from datetime import datetime, timezone

from data.news_sqlite import (
    NewsEvent,
    NewsStore,
    blackout_windows,
    is_news_blackout,
)


def _events() -> list[NewsEvent]:
    base = int(datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc).timestamp())
    return [
        NewsEvent(base, "Nonfarm Payrolls", "USD", "high"),
        NewsEvent(base + 3 * 3600, "FOMC Statement", "USA", "high"),
        NewsEvent(base + 6 * 3600, "Crude Oil Inventories", "USA", "medium"),
    ]


def test_store_roundtrip_and_idempotent_upsert(tmp_path):
    with NewsStore(tmp_path / "news.sqlite") as store:
        assert store.upsert_events(_events()) == 3
        # re-inserting the same natural key adds nothing
        assert store.upsert_events(_events()) == 0
        assert store.count() == 3


def test_events_between_filters_by_window_and_impact(tmp_path):
    events = _events()
    base = events[0].timestamp_utc
    with NewsStore(tmp_path / "news.sqlite") as store:
        store.upsert_events(events)
        found = store.events_between(base - 60, base + 60)
        assert [e.title for e in found] == ["Nonfarm Payrolls"]

        high_only = store.events_between(base - 60, base + 4 * 3600,
                                         impact_min="high")
        assert {e.title for e in high_only} == {"Nonfarm Payrolls",
                                                "FOMC Statement"}


def test_import_csv_roundtrip(tmp_path):
    csv_path = tmp_path / "calendar.csv"
    csv_path.write_text(
        "timestamp_utc,title,country,impact\n"
        "2026-08-20T12:30:00Z,Nonfarm Payrolls,USD,high\n"
        "2026-08-20 14:00:00,FOMC Statement,USA,high\n"
        "not,a,valid,row\n",
        encoding="utf-8")
    with NewsStore(tmp_path / "news.sqlite") as store:
        assert store.import_csv(csv_path) == 2
        assert store.count() == 2


def test_is_news_blackout_buffer_semantics():
    events = _events()
    ts = events[0].timestamp_utc

    assert is_news_blackout(ts, events, buffer_before_min=30,
                            buffer_after_min=30)
    # 31 minutes before the event: outside the +-30 window
    assert not is_news_blackout(ts - 31 * 60, events, buffer_before_min=30,
                                buffer_after_min=30)
    # 29 minutes before: inside
    assert is_news_blackout(ts - 29 * 60, events, buffer_before_min=30,
                            buffer_after_min=30)
    # 31 minutes after: outside
    assert not is_news_blackout(ts + 31 * 60, events, buffer_before_min=30,
                                buffer_after_min=30)
    # far away
    assert not is_news_blackout(ts + 86400, events, buffer_before_min=30,
                                buffer_after_min=30)
    # a datetime input behaves like the epoch form
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert is_news_blackout(dt, events)


def test_blackout_windows_are_merged_pairs():
    events = _events()
    # two events 30 min apart -> their +-30 min buffers merge into ONE window
    base = events[0].timestamp_utc
    pair = [NewsEvent(base, "A", "USD", "high"),
            NewsEvent(base + 1800, "B", "USD", "high")]
    windows = blackout_windows(pair, buffer_before_min=30,
                               buffer_after_min=30)
    assert len(windows) == 1
    assert windows[0] == (base - 1800, base + 1800 + 1800)

    # distant events stay separate
    far = _events()
    windows = blackout_windows(far, buffer_before_min=30,
                               buffer_after_min=30)
    assert len(windows) == 3
    for start, end in windows:
        assert end > start
