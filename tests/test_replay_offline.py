# -*- coding: utf-8 -*-
"""ТЗ §12.15: replay performs zero network calls; end-to-end signal on CSV."""
import socket

import pytest

from usstocks.replay import main as replay_main
from tests.fixtures.vwap_scenarios import (
    benchmark_uptrend,
    long_scenario,
    to_csv_rows,
)


@pytest.fixture
def csv_files(tmp_path):
    sym = tmp_path / "AMD.csv"
    bench = tmp_path / "QQQ.csv"
    sym.write_text("\n".join(to_csv_rows(long_scenario(), "AMD")), encoding="utf-8")
    bench.write_text("\n".join(to_csv_rows(benchmark_uptrend(), "QQQ")),
                     encoding="utf-8")
    return sym, bench


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """Any socket construction during the test means a network attempt."""
    def _no_socket(*a, **kw):
        raise AssertionError("replay must not touch the network")
    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)
    yield


@pytest.fixture
def signal_only_profile(monkeypatch):
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    monkeypatch.setattr("sys.argv", ["usstocks.replay"])
    yield


def test_replay_end_to_end_signal_offline(csv_files, signal_only_profile, capsys):
    sym, bench = csv_files
    rc = replay_main([
        "--symbol-csv", f"AMD={sym}",
        "--benchmark-csv", f"QQQ={bench}",
        "--watchlist", "AMD",
        "--is-tech", "AMD",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SIGNAL long" in out
    assert "PASS VWAP_TOUCH" in out or "PASS CONFIRM_CLOSE_VWAP" in out
    assert "shares" in out and "risk" in out


def test_replay_reports_failed_reasons(csv_files, signal_only_profile, capsys):
    sym, bench = csv_files
    rc = replay_main([
        "--symbol-csv", f"AMD={sym}",
        "--benchmark-csv", f"QQQ={bench}",
        "--watchlist", "TSLA",          # AMD not in watchlist -> must say why
        "--is-tech", "AMD",
    ])
    out = capsys.readouterr().out
    assert rc == 0 and "FAIL WATCHLIST_MEMBER" in out


def test_replay_refuses_legacy_profile(monkeypatch, csv_files):
    sym, bench = csv_files
    monkeypatch.delenv("PROFILE", raising=False)
    monkeypatch.setattr("sys.argv", ["usstocks.replay"])
    from usstocks.guards import EXIT_SIGNAL_ONLY
    with pytest.raises(SystemExit) as exc:
        replay_main(["--symbol-csv", f"AMD={sym}"])
    assert exc.value.code == EXIT_SIGNAL_ONLY
