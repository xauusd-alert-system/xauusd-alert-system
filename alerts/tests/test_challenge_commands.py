# -*- coding: utf-8 -*-
"""Tests for alerts/challenge_commands.py (Phase 5 part 2, Chunk A).

No live Telegram, no network. A StubBot records every (chat_id, text, parse_mode)
call. The heavy dependencies (``challenge.manual`` and ``pairs_analysis``) are
replaced with fake modules injected into ``sys.modules`` via the ``monkeypatch``
fixture, so real code / data files are never imported or touched.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import types

import pytest

from alerts import challenge_commands


# --------------------------------------------------------------------------- #
# Stub bot                                                                    #
# --------------------------------------------------------------------------- #
class StubBot:
    """Records Telegram sends without any network."""

    def __init__(self):
        self.calls = []

    def send(self, chat_id, text, parse_mode=""):
        self.calls.append((chat_id, text, parse_mode))

    def texts(self):
        return [t for _, t, _ in self.calls]

    def last_text(self):
        return self.calls[-1][1] if self.calls else None


# --------------------------------------------------------------------------- #
# Fake challenge.manual package                                               #
# --------------------------------------------------------------------------- #
class FakeState:
    def __init__(
        self,
        stage=2,
        profile="B",
        date="2026-08-30",
        current_equity=1050.0,
        day_start_equity=1000.0,
        trades_today=3,
        effective_max_trades=5,
        losses_today=1,
        effective_risk_usd=20.0,
        effective_only_a=False,
        status="ACTIVE",
        status_reason="ok",
        paused_until=None,
    ):
        self.stage = stage
        self.profile = profile
        self.date = date
        self.current_equity = current_equity
        self.day_start_equity = day_start_equity
        self.trades_today = trades_today
        self.effective_max_trades = effective_max_trades
        self.losses_today = losses_today
        self.effective_risk_usd = effective_risk_usd
        self.effective_only_a = effective_only_a
        self.status = status
        self.status_reason = status_reason
        self.paused_until = paused_until

    def daily_pnl(self):
        return self.current_equity - self.day_start_equity


class FakeDailyStateMachine:
    def __init__(self, **kw):
        self.state = FakeState(**kw)


class FakeRisk:
    DailyStateMachine = FakeDailyStateMachine


class FakeJournal:
    ROWS = [
        {
            "num": 1,
            "date": "2026-08-30",
            "time": "10:00",
            "instrument": "EURUSD",
            "direction": "L",
            "setup_class": "A",
            "entry_price": 1.1,
            "stop": 1.09,
            "outcome": "win",
            "result_usd": 50.0,
        }
    ]
    SUMMARY = [
        {
            "date": "2026-08-30",
            "trades": 1,
            "pnl_usd": 50.0,
            "win_rate_pct": 100.0,
            "avg_r": 1.5,
        }
    ]

    @staticmethod
    def read(path):
        return list(FakeJournal.ROWS)

    @staticmethod
    def daily_summary(path):
        return list(FakeJournal.SUMMARY)


class FakeAlerter:
    @staticmethod
    def refresh_access():
        return {"token": "t"}

    @staticmethod
    def scan_watchlist(access):
        return [{"grade": "A", "symbol": "EURUSD"}]

    @staticmethod
    def format_setup(res):
        return "SETUP-TEXT"


class FakeOutcomes:
    @staticmethod
    def format_stats_summary(stats):
        return "STATS-SUMMARY"

    @staticmethod
    def read_journal(path):
        return [{"x": 1}]

    @staticmethod
    def compute_stats(rows):
        return {"total": 1}

    @staticmethod
    def save_stats(path, stats):
        FakeOutcomes.saved = (path, stats)


def _mod(name, attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def fake_manual(monkeypatch):
    """Inject a fake ``challenge.manual`` package into sys.modules."""
    pkg = types.ModuleType("challenge.manual")
    journal_mod = _mod(
        "challenge.manual.journal",
        {
            "read": FakeJournal.read,
            "daily_summary": FakeJournal.daily_summary,
        },
    )
    risk_mod = _mod("challenge.manual.risk", {"DailyStateMachine": FakeDailyStateMachine})
    alerter_mod = _mod(
        "challenge.manual.alerter",
        {
            "refresh_access": FakeAlerter.refresh_access,
            "scan_watchlist": FakeAlerter.scan_watchlist,
            "format_setup": FakeAlerter.format_setup,
        },
    )
    outcomes_mod = _mod(
        "challenge.manual.outcomes",
        {
            "format_stats_summary": FakeOutcomes.format_stats_summary,
            "read_journal": FakeOutcomes.read_journal,
            "compute_stats": FakeOutcomes.compute_stats,
            "save_stats": FakeOutcomes.save_stats,
        },
    )
    pkg.journal = journal_mod
    pkg.risk = risk_mod
    pkg.alerter = alerter_mod
    pkg.outcomes = outcomes_mod
    for name, mod in {
        "challenge": types.ModuleType("challenge"),
        "challenge.manual": pkg,
        "challenge.manual.journal": journal_mod,
        "challenge.manual.risk": risk_mod,
        "challenge.manual.alerter": alerter_mod,
        "challenge.manual.outcomes": outcomes_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return {"journal": journal_mod, "risk": risk_mod, "alerter": alerter_mod, "outcomes": outcomes_mod}


@pytest.fixture
def fake_pairs(monkeypatch):
    """Inject a fake ``pairs_analysis`` package into sys.modules."""

    class FakeMeasure:
        def __init__(self, name):
            self.name = name
            self.n_bars = 200
            self.beta = 0.8
            self.ratio = 1.2
            self.half_life_days = 10.0
            self.adf_p = 0.01
            self.hurst = 0.45
            self.sigma = 2.5

        def analyze(self, tf):
            return self

    class FakeSig:
        def __init__(self, direction="long", z=2.5, valid=True, reason="edge"):
            self.direction = direction
            self.z = z
            self.valid = valid
            self.reason = reason

    class FakePairAnalyzer:
        def __init__(self, pair, analysis):
            self.pair = pair
            self.analysis = analysis

        def analyze(self, tf):
            name = self.pair.get("name", "")
            if "RAISE" in name:
                raise ValueError("boom-analyze")
            return FakeMeasure(name)

    class FakeSignalEngine:
        def __init__(self, thresholds, bt_cfg):
            self.thresholds = thresholds
            self.bt_cfg = bt_cfg

        def current(self, m):
            return FakeSig(direction="long", z=2.5, valid=True, reason="z>2")

    class FakeEngine:
        def __init__(self, name, direction="long", confidence=80.0):
            self.name = name
            self.direction = direction
            self.confidence = confidence

    class FakeEnsemble:
        def __init__(self, direction="long", confidence=82.0):
            self.direction = direction
            self.confidence = confidence
            self.engines = [FakeEngine("kalman"), FakeEngine("hurst")]

        def summary_line(self):
            return "ensemble ok"

    class FakeEnsembleEngine:
        def __init__(self, cfg):
            self.cfg = cfg

        def forecast(self, m):
            if "RAISE" in getattr(m, "name", ""):
                raise RuntimeError("ens boom")
            return FakeEnsemble()

    cfg = {
        "pairs": [{"name": "EURUSD/GBPUSD"}, {"name": "AUDUSD/NZDUSD"}],
        "analysis": {"default_timeframe": "D1", "lookback": 300},
        "thresholds": {"z_entry": 2.0},
        "backtest": {"window": 60},
    }
    pa_mod = _mod(
        "pairs_analysis",
        {
            "EnsembleEngine": FakeEnsembleEngine,
            "PairAnalyzer": FakePairAnalyzer,
            "SignalEngine": FakeSignalEngine,
            "load_config": lambda: cfg,
        },
    )
    integ_mod = _mod(
        "pairs_analysis.integrations",
        {
            "pair_cumulative_stats": lambda: {
                "total_trades": 5,
                "win_rate_pct": 50,
                "avg_r": 1.2,
                "sum_r": 6.0,
            }
        },
    )
    for name, mod in {
        "pairs_analysis": pa_mod,
        "pairs_analysis.integrations": integ_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return pa_mod


# --------------------------------------------------------------------------- #
# Helpers for file-path constants                                             #
# --------------------------------------------------------------------------- #
@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Point every module-level data-file constant at tmp_path."""
    mapping = {
        "STATE_FILE": "day_state.json",
        "SENT_FILE": "alerts_sent.json",
        "JOURNAL_FILE": "journal.csv",
        "STATS_FILE": "setup_stats.json",
        "OUTCOMES_CSV": "setup_outcomes.csv",
    }
    out = {}
    for const, fname in mapping.items():
        p = tmp_path / fname
        monkeypatch.setattr(challenge_commands, const, str(p))
        out[const] = p
    return out


@pytest.fixture
def today():
    return dt.datetime.now(dt.UTC).date().isoformat()


# --------------------------------------------------------------------------- #
# _import_manual                                                              #
# --------------------------------------------------------------------------- #
def test_import_manual_success(fake_manual):
    mods, err = challenge_commands._import_manual()
    assert err is None
    assert mods is not None


def test_import_manual_failure(monkeypatch):
    # Make ``challenge.manual`` resolve to a bare module with no submodules, so the
    # real lazy import in ``_import_manual`` raises -> returns (None, err).
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    mods, err = challenge_commands._import_manual()
    assert mods is None
    assert isinstance(err, str) and err


# --------------------------------------------------------------------------- #
# /day                                                                        #
# --------------------------------------------------------------------------- #
def test_cmd_day_unavailable(monkeypatch):
    bot = StubBot()
    monkeypatch.setattr(challenge_commands, "_import_manual", lambda: (None, "boom"))
    challenge_commands.cmd_day(bot.send, "123")
    assert "Challenge system unavailable" in bot.last_text()


def test_cmd_day_not_started(fake_manual, paths):
    bot = StubBot()
    # STATE_FILE points at a non-existent tmp path -> not started
    challenge_commands.cmd_day(bot.send, "123")
    assert "День ещё не начат" in bot.last_text()


def test_cmd_day_happy(fake_manual, paths):
    bot = StubBot()
    paths["STATE_FILE"].write_text("{}", encoding="utf-8")
    challenge_commands.cmd_day(bot.send, "123")
    txt = bot.last_text()
    assert "профиль" in txt and "PnL дня" in txt and "Статус" in txt


def test_cmd_day_paused(fake_manual, paths):
    bot = StubBot()
    paths["STATE_FILE"].write_text("{}", encoding="utf-8")
    # Force a paused state
    fake_manual["risk"].DailyStateMachine = lambda: type(
        "S", (), {"state": FakeState(paused_until="2026-08-30T12:00")}
    )()
    challenge_commands.cmd_day(bot.send, "123")
    assert "Пауза до" in bot.last_text()


# --------------------------------------------------------------------------- #
# /journal                                                                    #
# --------------------------------------------------------------------------- #
def test_cmd_journal_unavailable(monkeypatch):
    bot = StubBot()
    monkeypatch.setattr(challenge_commands, "_import_manual", lambda: (None, "boom"))
    challenge_commands.cmd_journal(bot.send, "123")
    assert "Challenge system unavailable" in bot.last_text()


def test_cmd_journal_empty_file(fake_manual, paths):
    bot = StubBot()
    # JOURNAL_FILE missing -> empty
    challenge_commands.cmd_journal(bot.send, "123")
    assert "Журнал пуст" in bot.last_text()


def test_cmd_journal_empty_rows(fake_manual, paths, monkeypatch):
    bot = StubBot()
    paths["JOURNAL_FILE"].write_text("x", encoding="utf-8")
    monkeypatch.setattr(fake_manual["journal"], "read", staticmethod(lambda p: []))
    challenge_commands.cmd_journal(bot.send, "123")
    assert "Журнал пуст" in bot.last_text()


def test_cmd_journal_happy_default(fake_manual, paths):
    bot = StubBot()
    paths["JOURNAL_FILE"].write_text("x", encoding="utf-8")
    challenge_commands.cmd_journal(bot.send, "123")
    assert "Последние сделки" in bot.last_text()
    assert bot.calls[-1][2] == "Markdown"


def test_cmd_journal_arg_n_clamped(fake_manual, paths):
    bot = StubBot()
    paths["JOURNAL_FILE"].write_text("x", encoding="utf-8")
    # "100" clamps to 20; no crash
    challenge_commands.cmd_journal(bot.send, "123", args=("100",))
    assert "Последние сделки" in bot.last_text()


def test_cmd_journal_arg_non_numeric(fake_manual, paths):
    bot = StubBot()
    paths["JOURNAL_FILE"].write_text("x", encoding="utf-8")
    challenge_commands.cmd_journal(bot.send, "123", args=("abc",))
    assert "Последние сделки" in bot.last_text()


# --------------------------------------------------------------------------- #
# /scan                                                                       #
# --------------------------------------------------------------------------- #
def test_cmd_scan_unavailable(monkeypatch):
    bot = StubBot()
    # Shadow the package with a bare module so the lazy import truly raises.
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    challenge_commands.cmd_scan(bot.send, "123")
    assert "Scanner unavailable" in bot.last_text()


def test_cmd_scan_error(fake_manual, monkeypatch):
    bot = StubBot()

    def _raise(*a, **k):
        raise RuntimeError("net")

    monkeypatch.setattr(fake_manual["alerter"], "refresh_access", staticmethod(_raise))
    challenge_commands.cmd_scan(bot.send, "123")
    assert "Scan error" in bot.last_text()


def test_cmd_scan_no_hits(fake_manual, monkeypatch):
    bot = StubBot()
    monkeypatch.setattr(fake_manual["alerter"], "scan_watchlist", staticmethod(lambda a: []))
    challenge_commands.cmd_scan(bot.send, "123")
    assert "Сетапов A/B сейчас нет" in bot.last_text()


def test_cmd_scan_hits(fake_manual):
    bot = StubBot()
    challenge_commands.cmd_scan(bot.send, "123")
    assert any("SETUP-TEXT" in t for _, t, _ in bot.calls)


# --------------------------------------------------------------------------- #
# /stats                                                                      #
# --------------------------------------------------------------------------- #
def test_cmd_stats_cache_hit(fake_manual, paths):
    bot = StubBot()
    paths["STATS_FILE"].write_text(json.dumps({"a": 1}), encoding="utf-8")
    challenge_commands.cmd_stats(bot.send, "123")
    assert "STATS-SUMMARY" in bot.last_text()


def test_cmd_stats_cache_broken_json(fake_manual, paths):
    bot = StubBot()
    paths["STATS_FILE"].write_text("{not json", encoding="utf-8")
    paths["OUTCOMES_CSV"].write_text("x", encoding="utf-8")
    challenge_commands.cmd_stats(bot.send, "123")
    assert "STATS-SUMMARY" in bot.last_text()


def test_cmd_stats_missing_compute(fake_manual, paths):
    bot = StubBot()
    paths["OUTCOMES_CSV"].write_text("x", encoding="utf-8")
    challenge_commands.cmd_stats(bot.send, "123")
    assert "STATS-SUMMARY" in bot.last_text()


def test_cmd_stats_no_outcomes_csv(fake_manual, paths):
    bot = StubBot()
    # OUTCOMES_CSV missing -> empty message
    challenge_commands.cmd_stats(bot.send, "123")
    assert "Журнал исходов пуст" in bot.last_text()


def test_cmd_stats_module_unavailable_compute(paths, monkeypatch):
    bot = StubBot()
    paths["OUTCOMES_CSV"].write_text("x", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    challenge_commands.cmd_stats(bot.send, "123")
    assert "Stats unavailable" in bot.last_text()


def test_cmd_stats_module_unavailable_cache(paths, monkeypatch):
    bot = StubBot()
    paths["STATS_FILE"].write_text(json.dumps({"a": 1}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    challenge_commands.cmd_stats(bot.send, "123")
    assert "Stats unavailable" in bot.last_text()


# --------------------------------------------------------------------------- #
# /pairs                                                                      #
# --------------------------------------------------------------------------- #
def test_cmd_pairs_unavailable(monkeypatch):
    bot = StubBot()
    # Shadow the package with a bare module so the lazy import truly raises.
    monkeypatch.setitem(sys.modules, "pairs_analysis", types.ModuleType("pairs_analysis"))
    challenge_commands.cmd_pairs(bot.send, "123")
    assert "Pairs module unavailable" in bot.last_text()


def test_cmd_pairs_config_error(fake_pairs, monkeypatch):
    bot = StubBot()
    monkeypatch.setattr(
        fake_pairs,
        "load_config",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("bad cfg"))),
    )
    challenge_commands.cmd_pairs(bot.send, "123")
    assert "Config error" in bot.last_text()


def test_cmd_pairs_happy(fake_pairs):
    bot = StubBot()
    challenge_commands.cmd_pairs(bot.send, "123")
    txt = bot.last_text()
    assert "PAIRS" in txt and "EURUSD/GBPUSD" in txt
    assert bot.calls[-1][2] == "Markdown"


def test_cmd_pairs_tf_arg(fake_pairs):
    bot = StubBot()
    challenge_commands.cmd_pairs(bot.send, "123", args=("h4",))
    assert "H4" in bot.last_text()


def test_cmd_pairs_cumulative_stats(fake_pairs):
    bot = StubBot()
    challenge_commands.cmd_pairs(bot.send, "123")
    assert "Pair stats" in bot.last_text()


def test_cmd_pairs_cumulative_import_error(fake_pairs, monkeypatch):
    bot = StubBot()
    monkeypatch.delitem(sys.modules, "pairs_analysis.integrations", raising=False)
    # Should not crash, just skip the cumulative block
    challenge_commands.cmd_pairs(bot.send, "123")
    assert "PAIRS" in bot.last_text()


def test_cmd_pairs_per_pair_error(fake_pairs):
    bot = StubBot()
    # Inject a pair whose analyzer raises
    fake_pairs.load_config = staticmethod(  # type: ignore[assignment]
        lambda: {
            "pairs": [{"name": "RAISE/PAIR"}],
            "analysis": {"default_timeframe": "D1"},
            "thresholds": {},
            "backtest": {},
        }
    )
    challenge_commands.cmd_pairs(bot.send, "123")
    assert "RAISE/PAIR" in bot.last_text() and "❌" in bot.last_text()


# --------------------------------------------------------------------------- #
# /alert                                                                      #
# --------------------------------------------------------------------------- #
def test_cmd_alert_no_file(fake_manual, paths):
    bot = StubBot()
    challenge_commands.cmd_alert(bot.send, "123")
    assert "Файл алертов не найден" in bot.last_text()


def test_cmd_alert_broken_json(fake_manual, paths):
    bot = StubBot()
    paths["SENT_FILE"].write_text("{broken", encoding="utf-8")
    challenge_commands.cmd_alert(bot.send, "123")
    # broken -> sent={}, but daily state section still appended
    assert "День:" in bot.last_text()


def test_cmd_alert_today(fake_manual, paths, today):
    bot = StubBot()
    key = today + "T10:00"
    paths["SENT_FILE"].write_text(
        json.dumps({key: {"grade": "A", "entry": 1.1, "target": 1.2}}),
        encoding="utf-8",
    )
    challenge_commands.cmd_alert(bot.send, "123")
    assert "A" in bot.last_text() and "День:" in bot.last_text()


def test_cmd_alert_state_unavailable(paths, monkeypatch):
    bot = StubBot()
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    challenge_commands.cmd_alert(bot.send, "123")
    assert "Состояние дня недоступно" in bot.last_text()
