"""Tests for the demo-only FX execution probe (execution/fx_execution_probe.py).

Contract pinned here:

1. execute_probe() input validation (asset allowlist, side, hold window,
   explicit FX_PROBE_CONFIRM for real orders);
2. the CSV row format (_append_row appends with headers exactly once);
3. FXProbeScheduler gating: disabled / session window / daily limit /
   min interval -> skip; eligible -> exactly one probe;
4. the full execute=True round trip against mt5_adapter.testing.MockMT5Module
   (entry order_send, position resolution by magic, close order_send,
   deal-history commission capture) — no live terminal, no real sleep.

Module-level ``mt5`` binding (resolved at import time) is monkeypatched on
the probe module itself; ``validate_symbol`` is stubbed the same way.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime

import pytest

import execution.fx_execution_probe as probe
from execution.fx_execution_probe import (
    CSV_FIELDS,
    PROBE_MAGIC,
    FXProbeScheduler,
    _append_row,
    _result_fields,
    execute_probe,
)
from mt5_adapter.testing import TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT, MockMT5Module, _DealTuple, _OrderResultTuple

SYMBOL = "EURUSD"
CSV = "probes.csv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_mt5(monkeypatch):
    """Replace the probe module's import-time mt5 handle with a MockMT5Module."""
    mock = MockMT5Module()  # trade_mode defaults to ACCOUNT_TRADE_MODE_DEMO
    mock.initialize()  # account_info() returns None until initialized
    monkeypatch.setattr(probe, "mt5", mock)
    monkeypatch.setattr(probe, "validate_symbol", lambda symbol: None)
    monkeypatch.setattr(probe, "initialize_mt5", lambda: None)
    monkeypatch.setattr(probe, "shutdown_mt5", lambda: None)
    return mock


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _probe_confirm_env(monkeypatch):
    """Real-order paths require the explicit YES confirmation."""
    monkeypatch.setenv("FX_PROBE_CONFIRM", "YES")


def _rejecting_result(request):
    return _OrderResultTuple(
        retcode=TRADE_RETCODE_REJECT,
        deal=0,
        order=0,
        volume=request.get("volume", 0.0),
        price=request.get("price", 0.0),
        comment="rejected",
        request_id=0,
        retcode_external=0,
    )


def _deals_for(position_ticket: int) -> list:
    return [
        _DealTuple(
            ticket=1,
            order=1,
            position_id=position_ticket,
            symbol=SYMBOL,
            type=0,
            entry=0,
            volume=0.01,
            price=1.1002,
            profit=0.0,
            commission=-0.10,
            swap=0.0,
            magic=PROBE_MAGIC,
            comment="in",
            time=0,
        ),
        _DealTuple(
            ticket=2,
            order=2,
            position_id=position_ticket,
            symbol=SYMBOL,
            type=1,
            entry=1,
            volume=0.01,
            price=1.1000,
            profit=0.15,
            commission=-0.10,
            swap=0.0,
            magic=PROBE_MAGIC,
            comment="out",
            time=0,
        ),
    ]


# ---------------------------------------------------------------------------
# 1. Validation (pure branches)
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("asset", ["XAUUSD", "us30", ""])
    def test_asset_not_in_allowlist(self, asset):
        with pytest.raises(ValueError, match="asset must be one of"):
            execute_probe(asset, "buy", 0.01, 2.0, CSV, execute=False)

    @pytest.mark.parametrize("side", ["long", "BUY", ""])
    def test_side_must_be_lowercase_buy_or_sell(self, side):
        with pytest.raises(ValueError, match="side must be buy or sell"):
            execute_probe(SYMBOL, side, 0.01, 2.0, CSV, execute=False)

    @pytest.mark.parametrize("hold", [0.0, -1.0, 5.1])
    def test_hold_seconds_window(self, hold):
        with pytest.raises(ValueError, match="hold_seconds must be"):
            execute_probe(SYMBOL, "buy", 0.01, hold, CSV, execute=False)

    def test_execute_without_confirm_refuses(self, monkeypatch):
        monkeypatch.delenv("FX_PROBE_CONFIRM", raising=False)  # overrides autouse fixture
        with pytest.raises(RuntimeError, match="FX_PROBE_CONFIRM"):
            execute_probe(SYMBOL, "buy", 0.01, 2.0, CSV, execute=True)

    def test_validation_happens_before_connection(self, mock_mt5):
        """A rejected call must never touch MT5 at all."""
        with pytest.raises(ValueError):
            execute_probe("XAUUSD", "buy", 0.01, 2.0, CSV, execute=False)
        assert mock_mt5.call_count("symbol_info") == 0


# ---------------------------------------------------------------------------
# 2. _result_fields
# ---------------------------------------------------------------------------


class TestResultFields:
    def test_extracts_fill_retcode_comment(self):
        result = _OrderResultTuple(
            retcode=10009,
            deal=1,
            order=2,
            volume=0.01,
            price=1.1002,
            comment="done",
            request_id=1,
            retcode_external=0,
        )
        fields = _result_fields(result, "entry")
        assert fields == {
            "entry_fill_price": 1.1002,
            "entry_retcode": 10009,
            "entry_comment": "done",
        }

    def test_none_result_gives_none_fields(self):
        fields = _result_fields(None, "close")
        assert fields == {
            "close_fill_price": None,
            "close_retcode": None,
            "close_comment": None,
        }


# ---------------------------------------------------------------------------
# 3. _append_row (CSV writer)
# ---------------------------------------------------------------------------


class TestAppendRow:
    def test_creates_file_with_headers(self, tmp_path):
        path = tmp_path / "sub" / CSV
        _append_row(str(path), {"asset": SYMBOL, "side": "buy", "status": "dry_run"})
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0] == CSV_FIELDS
        assert len(rows) == 2

    def test_appends_to_existing_file_without_duplicating_headers(self, tmp_path):
        path = tmp_path / CSV
        _append_row(str(path), {"asset": SYMBOL, "side": "buy", "status": "dry_run"})
        _append_row(str(path), {"asset": "GBPUSD", "side": "sell", "status": "dry_run"})
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 data rows
        assert rows[1][1] == SYMBOL
        assert rows[2][1] == "GBPUSD"

    def test_extra_keys_ignored_and_values_escaped(self, tmp_path):
        path = tmp_path / CSV
        _append_row(str(path), {"asset": "a,b", "note": "ignored-extra", "status": "x"})
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[1][CSV_FIELDS.index("asset")] == "a,b"
        assert "ignored-extra" not in rows[1]


# ---------------------------------------------------------------------------
# 4. FXProbeScheduler
# ---------------------------------------------------------------------------


def _install_clock(monkeypatch, hour: int):
    """Pin probe.datetime.now(UTC) to a fixed UTC hour."""
    fixed = datetime(2026, 8, 29, hour, 0, tzinfo=UTC)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG004 - signature mirror
            return fixed if tz is UTC else fixed.replace(tzinfo=None)

    monkeypatch.setattr(probe, "datetime", _FakeDateTime)
    return fixed


def _sched(**overrides) -> FXProbeScheduler:
    cfg = {
        "execution": {
            "fx_execution_probes": {
                "enabled": True,
                "assets": ["EURUSD", "GBPUSD"],
                "volume": 0.01,
                "hold_seconds": 2.0,
                "min_interval_minutes": 120,
                "max_probes_per_asset_per_day": 2,
                "max_spread_pips": {"EURUSD": 3.0},
                **overrides.get("probe", {}),
            }
        }
    }
    return FXProbeScheduler(cfg)


class TestSchedulerConstruction:
    def test_fields_from_config(self):
        s = _sched()
        assert s.enabled is True
        assert s.assets == ["EURUSD", "GBPUSD"]
        assert s.volume == 0.01
        assert s.hold_seconds == 2.0
        assert s.interval_seconds == 120 * 60
        assert s.daily_limit == 2
        assert s.max_spread_pips == {"EURUSD": 3.0}
        assert s.last_probe_at == {"EURUSD": 0.0, "GBPUSD": 0.0}

    def test_disabled(self):
        s = _sched(probe={"enabled": False})
        assert s.enabled is False


class TestEligibleSession:
    def test_london_hours_eligible(self, monkeypatch):
        _install_clock(monkeypatch, hour=9)
        assert _sched()._eligible_session() is True

    def test_ny_hours_eligible(self, monkeypatch):
        _install_clock(monkeypatch, hour=20)
        assert _sched()._eligible_session() is True

    def test_early_morning_not_eligible(self, monkeypatch):
        _install_clock(monkeypatch, hour=7)
        assert _sched()._eligible_session() is False

    def test_late_evening_not_eligible(self, monkeypatch):
        _install_clock(monkeypatch, hour=22)
        assert _sched()._eligible_session() is False


class TestMaybeRun:
    def test_disabled_returns_none(self, monkeypatch):
        _install_clock(monkeypatch, hour=10)
        calls = []
        monkeypatch.setattr(probe, "execute_probe", lambda **kw: calls.append(kw))
        s = _sched(probe={"enabled": False})
        assert s.maybe_run() is None
        assert calls == []

    def test_out_of_session_returns_none(self, monkeypatch):
        _install_clock(monkeypatch, hour=3)
        s = _sched()
        assert s.maybe_run() is None

    def test_daily_limit_skips_asset_and_round_robins(self, monkeypatch):
        _install_clock(monkeypatch, hour=10)
        s = _sched()
        # Both assets exhausted for today.
        day = probe.datetime.now(UTC).date().isoformat()
        s.daily_counts = {(day, "EURUSD"): 2, (day, "GBPUSD"): 2}
        assert s.maybe_run() is None

    def test_min_interval_not_elapsed_returns_none(self, monkeypatch):
        _install_clock(monkeypatch, hour=10)
        s = _sched()
        recent = probe.time.time() - 60  # 1 min ago < 120 min
        s.last_probe_at["EURUSD"] = recent
        s.last_probe_at["GBPUSD"] = recent
        assert s.maybe_run() is None

    def test_eligible_runs_probe_and_counts(self, monkeypatch):
        _install_clock(monkeypatch, hour=10)
        calls = []
        monkeypatch.setattr(
            probe,
            "execute_probe",
            lambda **kw: calls.append(kw) or {"asset": kw["asset"], "status": "closed"},
        )
        s = _sched()
        row = s.maybe_run()
        assert row == {"asset": "EURUSD", "status": "closed"}
        assert len(calls) == 1
        assert calls[0]["execute"] is True
        assert calls[0]["manage_connection"] is False
        assert calls[0]["max_spread_pips"] == 3.0
        day = probe.datetime.now(UTC).date().isoformat()
        assert s.daily_counts == {(day, "EURUSD"): 1}
        # First probe is a buy (even count).
        assert calls[0]["side"] == "buy"

    def test_second_probe_alternates_side(self, monkeypatch):
        _install_clock(monkeypatch, hour=10)
        calls = []
        monkeypatch.setattr(
            probe,
            "execute_probe",
            lambda **kw: calls.append(kw) or {"asset": kw["asset"], "status": "closed"},
        )
        s = _sched()
        s.interval_seconds = 0  # allow back-to-back
        s.maybe_run()
        s.maybe_run()
        assert calls[1]["asset"] == "GBPUSD"
        assert calls[1]["side"] == "buy"

    def test_exception_marks_skipped_and_backs_off(self, monkeypatch):
        _install_clock(monkeypatch, hour=10)
        calls = []

        def _boom(**kw):
            calls.append(kw)
            raise RuntimeError("no quote")

        monkeypatch.setattr(probe, "execute_probe", _boom)
        s = _sched()
        row = s.maybe_run()
        assert row == {"asset": "EURUSD", "status": "skipped", "reason": "no quote"}
        assert s.last_probe_at["EURUSD"] > 0  # back-off recorded
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 5. Happy path execute_probe(execute=True)
# ---------------------------------------------------------------------------


class TestExecuteProbeRoundTrip:
    def test_full_round_trip(self, mock_mt5, no_sleep, tmp_path, monkeypatch):
        csv_path = tmp_path / CSV
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001, volume_min=0.01)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        order_sends = []

        def handler(request):
            order_sends.append(request)
            if "position" not in request:
                # Entry: create the probe position (magic = PROBE_MAGIC).
                mock_mt5.add_position(SYMBOL, type=mock_mt5.ORDER_TYPE_BUY, volume=0.01, magic=PROBE_MAGIC)
                pos = mock_mt5.positions[-1]
                mock_mt5.deals[pos.ticket] = _deals_for(pos.ticket)
            return _OrderResultTuple(
                retcode=TRADE_RETCODE_DONE,
                deal=1,
                order=1,
                volume=request.get("volume", 0.0),
                price=request.get("price", 0.0),
                comment="",
                request_id=1,
                retcode_external=0,
            )

        mock_mt5.order_send_handler = handler
        row = execute_probe(
            SYMBOL, "buy", 0.01, 2.0, str(csv_path), execute=True, manage_connection=True
        )

        assert row["status"] == "closed"
        assert row["entry_retcode"] == TRADE_RETCODE_DONE
        assert row["close_retcode"] == TRADE_RETCODE_DONE
        assert row["position_ticket"] == mock_mt5.positions[0].ticket
        assert row["entry_requested_price"] == pytest.approx(1.1002)
        # Deal history: commissions and realized profit captured.
        assert row["total_commission"] == pytest.approx(-0.20)
        assert row["realized_profit"] == pytest.approx(-0.05)  # 0.15 - 0.20
        # Two order_send calls: open then close; close carries the position id.
        assert len(order_sends) == 2
        assert "position" in order_sends[1]
        assert len(mock_mt5.calls) > 0
        # CSV written exactly once with the final row.
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["status"] == "closed"

    def test_entry_rejected_recorded_without_close(self, mock_mt5, tmp_path):
        csv_path = tmp_path / CSV
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        order_sends = []
        mock_mt5.order_send_handler = lambda request: order_sends.append(request) or _rejecting_result(request)

        row = execute_probe(SYMBOL, "buy", 0.01, 1.0, str(csv_path), execute=True)
        assert row["status"] == "entry_rejected"
        assert row["entry_retcode"] == TRADE_RETCODE_REJECT
        assert len(order_sends) == 1  # no close attempt

    def test_entry_unresolved_raises(self, mock_mt5, no_sleep, tmp_path):
        """Entry filled but the probe position cannot be resolved."""
        csv_path = tmp_path / CSV
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        # Foreign position (different magic) exists after the fill.
        def handler(request):
            if "position" not in request:
                mock_mt5.add_position(SYMBOL, type=0, volume=0.01, magic=42)
            return _OrderResultTuple(
                retcode=TRADE_RETCODE_DONE, deal=1, order=1, volume=0.01,
                price=1.0, comment="", request_id=1, retcode_external=0,
            )

        mock_mt5.order_send_handler = handler
        with pytest.raises(RuntimeError, match="could not be resolved"):
            execute_probe(SYMBOL, "buy", 0.01, 1.0, str(csv_path), execute=True)

    def test_close_rejected_status(self, mock_mt5, no_sleep, tmp_path):
        csv_path = tmp_path / CSV
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        state = {"entry_done": False}

        def handler(request):
            if "position" not in request and not state["entry_done"]:
                state["entry_done"] = True
                mock_mt5.add_position(SYMBOL, type=0, volume=0.01, magic=PROBE_MAGIC)
                return _OrderResultTuple(
                    retcode=TRADE_RETCODE_DONE, deal=1, order=1, volume=0.01,
                    price=1.0, comment="", request_id=1, retcode_external=0,
                )
            return _rejecting_result(request)

        mock_mt5.order_send_handler = handler
        row = execute_probe(SYMBOL, "buy", 0.01, 1.0, str(csv_path), execute=True)
        assert row["status"] == "close_rejected"

    def test_close_rejected_commission_absent(self, mock_mt5, no_sleep, tmp_path):
        csv_path = tmp_path / CSV
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        state = {"entry_done": False}

        def handler(request):
            if "position" not in request and not state["entry_done"]:
                state["entry_done"] = True
                mock_mt5.add_position(SYMBOL, type=0, volume=0.01, magic=PROBE_MAGIC)
                return _OrderResultTuple(
                    retcode=TRADE_RETCODE_DONE, deal=1, order=1, volume=0.01,
                    price=1.0, comment="", request_id=1, retcode_external=0,
                )
            return _rejecting_result(request)

        mock_mt5.order_send_handler = handler
        row = execute_probe(SYMBOL, "buy", 0.01, 1.0, str(csv_path), execute=True)
        assert row["status"] == "close_rejected"
        assert "total_commission" not in row


# ---------------------------------------------------------------------------
# 6. Edge cases (pre-trade refusals)
# ---------------------------------------------------------------------------


class TestExecuteProbeEdgeCases:
    def test_non_demo_account_refused(self, mock_mt5):

        mock_mt5.account = mock_mt5.account._replace(trade_mode=1)  # real account
        with pytest.raises(RuntimeError, match="not DEMO"):
            execute_probe(SYMBOL, "buy", 0.01, 1.0, CSV, execute=True, manage_connection=False)

    def test_open_position_on_symbol_refused(self, mock_mt5):
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        mock_mt5.add_position(SYMBOL, type=0, volume=0.01, magic=42)
        with pytest.raises(RuntimeError, match="already exists"):
            execute_probe(SYMBOL, "buy", 0.01, 1.0, CSV, execute=True, manage_connection=False)

    def test_volume_below_broker_minimum(self, mock_mt5):
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001, volume_min=0.10)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        with pytest.raises(ValueError, match="below broker minimum"):
            execute_probe(SYMBOL, "buy", 0.01, 1.0, CSV, execute=True, manage_connection=False)

    def test_no_quote_refused(self, mock_mt5):
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        # No tick configured -> symbol_info_tick returns None.
        with pytest.raises(RuntimeError, match="No quote"):
            execute_probe(SYMBOL, "buy", 0.01, 1.0, CSV, execute=True, manage_connection=False)

    def test_symbol_info_none_refused(self, mock_mt5):
        # No symbol_info configured -> info is None.
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        with pytest.raises(RuntimeError, match="Cannot read specification"):
            execute_probe(SYMBOL, "buy", 0.01, 1.0, CSV, execute=True, manage_connection=False)

    def test_spread_limit_refused(self, mock_mt5):
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1020)  # 20 pips
        with pytest.raises(RuntimeError, match="exceeds"):
            execute_probe(
                SYMBOL, "buy", 0.01, 1.0, CSV, execute=True,
                manage_connection=False, max_spread_pips=3.0,
            )

    def test_dry_run_records_row_without_orders(self, mock_mt5, tmp_path):
        csv_path = tmp_path / CSV
        mock_mt5.set_symbol_info(SYMBOL, digits=5, point=0.00001)
        mock_mt5.set_tick(SYMBOL, bid=1.1000, ask=1.1002)
        row = execute_probe(SYMBOL, "sell", 0.01, 1.0, str(csv_path), execute=False)
        assert row["status"] == "dry_run"
        assert mock_mt5.call_count("order_send") == 0
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1 and rows[0]["side"] == "sell"
