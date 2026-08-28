import sqlite3

import pytest

from config.deployment import order_routing_allowed
from contracts.signal_spec import SignalSpec, SignalState, TargetLeg
from data.channel_archive import import_archive
from data.news_filter import news_guard_decision
from data.signal_lifecycle import latest_signal_state, transition_signal
from data.trading_event_ledger import append_trading_event, verify_event_chain


def test_signal_spec_supports_arbitrary_target_legs_and_latency():
    spec = SignalSpec(
        strategy_version="v3", asset_key="XAUUSD", direction="long",
        state=SignalState.CONFIRMED, setup_timeframe="M15", context_timeframe="H1",
        created_at_utc=100, published_at_utc=112, zone_low=2000, zone_high=2001,
        stop_price=1990, targets=[TargetLeg(price=2010, close_ratio=.4),
                                 TargetLeg(price=2020, close_ratio=.3),
                                 TargetLeg(price=2030, close_ratio=.2),
                                 TargetLeg(price=2040, close_ratio=.1)],
        confirmed_by="systematic:ensemble", config_hash="abc",
    )
    assert len(spec.targets) == 4 and spec.publish_latency_seconds == 12
    assert len(spec.canonical_hash()) == 64


def test_deployment_modes_fail_closed():
    assert order_routing_allowed({"deployment": {"mode": "research"}})[0] is False
    assert order_routing_allowed({"deployment": {"mode": "human_confirmed"}})[0] is False
    assert order_routing_allowed({"deployment": {"mode": "human_confirmed"}}, confirmed_by="admin:1")[0] is True
    assert order_routing_allowed({"deployment": {"mode": "live_systematic"}})[0] is False


def test_primary_event_ledger_is_chained_and_append_only(tmp_path):
    db = str(tmp_path / "events.sqlite")
    common = dict(db_path=db, signal_id="s1", asset_key="XAUUSD",
                  strategy_version="v3", config_hash="cfg", actor="test")
    append_trading_event(event_type="signal_created", payload={"state": "watch"}, **common)
    append_trading_event(event_type="signal_armed", payload={"state": "armed"}, **common)
    assert verify_event_chain(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM trading_events")


def test_setup_lifecycle_rejects_skipped_confirmation_states(tmp_path):
    db = str(tmp_path / "lifecycle.sqlite")
    common = dict(db_path=db, signal_id="s2", asset_key="XAUUSD",
                  strategy_version="v3", config_hash="cfg", actor="system", reason="test")
    transition_signal(new_state="watch", **common)
    with pytest.raises(ValueError, match="invalid signal transition"):
        transition_signal(new_state="confirmed", **common)
    transition_signal(new_state="armed", **common)
    transition_signal(new_state="confirmed", **common)
    assert latest_signal_state(db, "s2") == "confirmed"


def test_telegram_archive_stays_unlinked_and_idempotent(tmp_path):
    html = tmp_path / "messages.html"
    html.write_text('''<div class="message default clearfix" id="message42">
      <div class="date details" title="14.08.2026 12:00:00 UTC+00:00"></div>
      <div class="text">#hehe XAUUSD TP1</div></div>''', encoding="utf-8")
    db = str(tmp_path / "archive.sqlite")
    assert import_archive(db, str(html)) == 1
    assert import_archive(db, str(html)) == 0
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT linkage_status,linked_signal_id FROM channel_archive_messages").fetchone()
    assert row == ("unlinked", None)


def test_historical_news_calendar_is_dated_and_symmetric(tmp_path):
    path = tmp_path / "news.csv"
    path.write_text("timestamp_utc,title,country,impact\n100000,FOMC,USD,High\n", encoding="utf-8")
    blocked, reason, available = news_guard_decision(
        100000, historical_calendar_path=str(path)
    )
    assert blocked and available and "FOMC" in reason
