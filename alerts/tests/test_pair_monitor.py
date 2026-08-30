# -*- coding: utf-8 -*-
"""Tests for alerts/pair_monitor.py (Phase 5 part 2, Chunks B + C).

Chunk B (pure / logic):
    _format_alert, _load_json / _save_json (tmp_path), _resolve_outcomes,
    _check_signals (with mocked MT5/API).
Chunk C (lazy imports):
    query_all + _ensure_imports (mocked ``pairs_analysis`` / ``challenge.manual``).

KEY RULE: PairMonitor is ALWAYS constructed with EXPLICIT test config paths
(``config_path=`` / ``pairs_config_path=``) pointing at tmp YAML files. The
default real ``ROOT/...`` configs are NEVER used. Module-level data-file
constants (PAIR_SENT_FILE, ...) are redirected to tmp_path too, so no real
``data/manual/*`` files are touched. Run loops (``_loop`` / ``_poll_once`` /
``start`` / ``stop``) are never invoked.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import types

import pytest
import yaml

from alerts import pair_monitor
from alerts.pair_monitor import PairMonitor, _load_json, _save_json


# --------------------------------------------------------------------------- #
# Fakes for pairs_analysis                                                    #
# --------------------------------------------------------------------------- #
class FakeMeasure:
    def __init__(
        self,
        name,
        half_life_days=10.0,
        beta=0.8,
        ratio=1.2,
        adf_p=0.01,
        hurst=0.45,
        sigma=2.5,
        n_bars=200,
    ):
        self.name = name
        self.half_life_days = half_life_days
        self.beta = beta
        self.ratio = ratio
        self.adf_p = adf_p
        self.hurst = hurst
        self.sigma = sigma
        self.n_bars = n_bars

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


class _RaisingEnsembleEngine:
    def __init__(self, cfg):
        self.cfg = cfg

    def forecast(self, m):
        raise RuntimeError("ens boom")


def _mod(name, attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def fake_pairs(monkeypatch):
    pa = _mod(
        "pairs_analysis",
        {
            "EnsembleEngine": FakeEnsembleEngine,
            "PairAnalyzer": FakePairAnalyzer,
            "SignalEngine": FakeSignalEngine,
            "load_config": lambda: {
                "pairs": [{"name": "X/Y"}],
                "analysis": {},
                "thresholds": {},
                "backtest": {},
            },
        },
    )
    integ = _mod(
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
    for n, m in {"pairs_analysis": pa, "pairs_analysis.integrations": integ}.items():
        monkeypatch.setitem(sys.modules, n, m)
    return pa


@pytest.fixture
def fake_manual(monkeypatch):
    po = _mod("challenge.manual.pair_outcomes", {"resolve_pair_outcomes": lambda tf: None})
    oc = _mod(
        "challenge.manual.outcomes",
        {
            "compute_stats": lambda rows: {"total": 5, "win_rate": 50, "avg_r": 1.2},
            "read_journal": lambda path: [{"x": 1}],
        },
    )
    pkg = types.ModuleType("challenge.manual")
    pkg.pair_outcomes = po
    pkg.outcomes = oc
    for n, m in {
        "challenge": types.ModuleType("challenge"),
        "challenge.manual": pkg,
        "challenge.manual.pair_outcomes": po,
        "challenge.manual.outcomes": oc,
    }.items():
        monkeypatch.setitem(sys.modules, n, m)
    return {"pair_outcomes": po, "outcomes": oc}


# --------------------------------------------------------------------------- #
# Config + path fixtures (ALWAYS tmp)                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture
def manual_config(tmp_path):
    p = tmp_path / "manual_config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "pair_alerts": {
                    "enabled": True,
                    "timeframe": "H1",
                    "poll_minutes": 55,
                    "alert_cooldown_hours": 23,
                    # no "pairs" key -> pairs_to_watch == [] (watch all)
                }
            }
        ),
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def pairs_config(tmp_path):
    p = tmp_path / "pairs_config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "pairs": [{"name": "EURUSD/GBPUSD"}, {"name": "AUDUSD/NZDUSD"}],
                "analysis": {"default_timeframe": "H1", "lookback": 300},
                "thresholds": {"z_entry": 2.0},
                "backtest": {"window": 60},
            }
        ),
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def pair_paths(tmp_path, monkeypatch):
    mapping = {
        "PAIR_SENT_FILE": "pair_alerts_sent.json",
        "PAIR_RESOLVED_FILE": "pair_outcomes_resolved.json",
        "PAIR_JOURNAL_CSV": "pair_journal.csv",
        "PAIR_STATS_FILE": "pair_outcomes_stats.json",
    }
    out = {}
    for const, fname in mapping.items():
        fp = tmp_path / fname
        monkeypatch.setattr(pair_monitor, const, str(fp))
        out[const] = fp
    return out


def make_monitor(manual_config, pairs_config, send_fn=None, **kw):
    return PairMonitor(
        send_fn if send_fn is not None else (lambda *a, **k: None),
        config_path=manual_config,
        pairs_config_path=pairs_config,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Chunk B: __init__ edge cases                                                #
# --------------------------------------------------------------------------- #
def test_init_missing_config_files(tmp_path):
    m = PairMonitor(
        lambda *a, **k: None,
        config_path=str(tmp_path / "no_manual.yaml"),
        pairs_config_path=str(tmp_path / "no_pairs.yaml"),
    )
    assert m.cfg == {}
    assert m.pairs_cfg == {}
    assert m.enabled is False


# --------------------------------------------------------------------------- #
# Chunk B: _format_alert                                                      #
# --------------------------------------------------------------------------- #
def test_format_alert_long_with_ensemble(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(
        FakeSig(direction="long", z=2.6),
        FakeMeasure("EURUSD/GBPUSD"),
        FakeEnsemble(),
    )
    assert "LONG" in text and "ENSEMBLE" in text and "EURUSD/GBPUSD" in text


def test_format_alert_short_no_ensemble(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(direction="short", z=1.5), FakeMeasure("EURUSD/GBPUSD"), None)
    assert "SHORT" in text and "ENSEMBLE" not in text


def test_format_alert_extreme_z(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(z=3.2), FakeMeasure("EURUSD/GBPUSD"), None)
    assert "EXTREME" in text


def test_format_alert_strong_z(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(z=2.5), FakeMeasure("EURUSD/GBPUSD"), None)
    assert "STRONG" in text


def test_format_alert_normal_z(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(z=1.5), FakeMeasure("EURUSD/GBPUSD"), None)
    assert "NORMAL" in text


def test_format_alert_half_life_hours(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(), FakeMeasure("EURUSD/GBPUSD", half_life_days=0.05), None)
    assert "1h" in text


def test_format_alert_hurst_mr(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(), FakeMeasure("EURUSD/GBPUSD", hurst=0.45), None)
    assert "MR" in text


def test_format_alert_hurst_trend(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    text = m._format_alert(FakeSig(), FakeMeasure("EURUSD/GBPUSD", hurst=0.6), None)
    assert "Trend" in text


# --------------------------------------------------------------------------- #
# Chunk B: _load_json / _save_json                                            #
# --------------------------------------------------------------------------- #
def test_load_json_missing(tmp_path):
    assert _load_json(str(tmp_path / "nope.json")) == {}


def test_load_json_valid(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert _load_json(str(p)) == {"a": 1}


def test_load_json_broken(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{bad json", encoding="utf-8")
    assert _load_json(str(p)) == {}


def test_save_json_roundtrip(tmp_path):
    p = tmp_path / "y.json"
    _save_json(str(p), {"k": "v"})
    assert _load_json(str(p)) == {"k": "v"}


def test_save_json_creates_dirs(tmp_path):
    p = tmp_path / "sub" / "deep" / "y.json"
    _save_json(str(p), {"k": 2})
    assert _load_json(str(p)) == {"k": 2}


# --------------------------------------------------------------------------- #
# Chunk B: _resolve_outcomes                                                  #
# --------------------------------------------------------------------------- #
def test_resolve_outcomes_ok(manual_config, pairs_config, fake_manual):
    m = make_monitor(manual_config, pairs_config)
    captured = {}
    fake_manual["pair_outcomes"].resolve_pair_outcomes = lambda tf: captured.setdefault("tf", tf)
    m._resolve_outcomes()
    assert captured.get("tf") == m.timeframe


def test_resolve_outcomes_import_error(manual_config, pairs_config, monkeypatch):
    m = make_monitor(manual_config, pairs_config)
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    # import of challenge.manual.pair_outcomes fails -> swallowed, no raise
    m._resolve_outcomes()


# --------------------------------------------------------------------------- #
# Chunk B: _check_signals                                                     #
# --------------------------------------------------------------------------- #
def test_check_signals_disabled(manual_config, pairs_config):
    m = make_monitor(manual_config, pairs_config)
    m.enabled = False
    assert m._check_signals() == []


def test_check_signals_valid(manual_config, pairs_config, fake_pairs, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    alerts = m._check_signals()
    assert len(alerts) == 2
    assert all(isinstance(a, tuple) and len(a) == 3 for a in alerts)
    # metadata built with ensemble direction
    assert alerts[0][2]["ensemble_direction"] == "long"


def test_check_signals_cooldown(manual_config, pairs_config, fake_pairs, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    recent = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat()
    _save_json(str(pair_paths["PAIR_SENT_FILE"]), {"EURUSD/GBPUSD": {"sent_at": recent}})
    alerts = m._check_signals()
    names = [a[0] for a in alerts]
    assert "EURUSD/GBPUSD" not in names
    assert "AUDUSD/NZDUSD" in names


def test_check_signals_cooldown_broken_sent(manual_config, pairs_config, fake_pairs, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    # unparseable sent_at -> except branch (214-215), pair still checked
    _save_json(str(pair_paths["PAIR_SENT_FILE"]), {"EURUSD/GBPUSD": {"sent_at": "not-a-date"}})
    alerts = m._check_signals()
    assert "EURUSD/GBPUSD" in [a[0] for a in alerts]


def test_check_signals_not_watched(manual_config, pairs_config, fake_pairs, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    m.pairs_to_watch = ["otherpair"]
    assert m._check_signals() == []


def test_check_signals_analyzer_raises(manual_config, pairs_config, fake_pairs, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    m.pairs_cfg = {
        "pairs": [{"name": "RAISE/PAIR"}],
        "analysis": {},
        "thresholds": {},
        "backtest": {},
    }
    assert m._check_signals() == []


def test_check_signals_ensemble_raises(manual_config, pairs_config, fake_pairs, pair_paths, monkeypatch):
    monkeypatch.setattr(fake_pairs, "EnsembleEngine", _RaisingEnsembleEngine)
    m = make_monitor(manual_config, pairs_config)
    alerts = m._check_signals()
    assert len(alerts) == 2
    assert alerts[0][2]["ensemble_direction"] == "neutral"


# --------------------------------------------------------------------------- #
# Chunk C: _ensure_imports                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.chunk_c
def test_ensure_imports_idempotent(manual_config, pairs_config, fake_pairs, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    m._ensure_imports()
    assert m._pair_analyzer is not None
    before = m._pair_analyzer
    m._ensure_imports()  # early return, no re-import
    assert m._pair_analyzer is before


@pytest.mark.chunk_c
def test_ensure_imports_loads_pairs_config(manual_config, fake_pairs, pair_paths, tmp_path):
    bad = str(tmp_path / "does_not_exist.yaml")
    m = PairMonitor(lambda *a, **k: None, config_path=manual_config, pairs_config_path=bad)
    assert m.pairs_cfg == {}
    m._ensure_imports()
    assert m.pairs_cfg.get("pairs")
    assert m._pair_analyzer is not None


# --------------------------------------------------------------------------- #
# Chunk C: query_all                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.chunk_c
def test_query_all_basic(manual_config, pairs_config, fake_pairs, fake_manual, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    text = m.query_all()
    assert "PAIRS" in text
    assert "EURUSD/GBPUSD" in text
    assert "AUDUSD/NZDUSD" in text
    assert "Pair stats" in text


@pytest.mark.chunk_c
def test_query_all_pair_error(manual_config, pairs_config, fake_pairs, fake_manual, pair_paths):
    m = make_monitor(manual_config, pairs_config)
    m.pairs_cfg = {
        "pairs": [{"name": "RAISE/PAIR"}],
        "analysis": {},
        "thresholds": {},
        "backtest": {},
    }
    text = m.query_all()
    assert "RAISE/PAIR" in text and "ERROR" in text


@pytest.mark.chunk_c
def test_query_all_stats_import_error(manual_config, pairs_config, fake_pairs, pair_paths, monkeypatch):
    m = make_monitor(manual_config, pairs_config)
    monkeypatch.setitem(sys.modules, "challenge", types.ModuleType("challenge"))
    monkeypatch.setitem(sys.modules, "challenge.manual", types.ModuleType("challenge.manual"))
    # outcomes import fails -> except pass, query still returns
    text = m.query_all()
    assert "PAIRS" in text
