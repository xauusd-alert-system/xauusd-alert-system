"""
Rolling hold-out gate tests (Rolling Holdout policy, Step 2 - 2026-08-30).

Covers:
  * released_windows - the pure slice filter (scoring only inside [old, new)).
  * propose - the isolated released-window gate, with a hard no-peeking proof
    (R4): the candidate is never trained on data at/after new_start, and nothing
    is scored outside [old_start, new_start).
  * journal - propose / move / rollback each append an append-only CSV row.
  * CI guard (scripts/check_holdout_roll.py) - config.locked_holdout.start must
    equal the journal's effective current start.

Tests are pure unit tests: heavy dependencies (SQLite read_candles, feature
building, the ensemble backtester, the model predictor) are monkeypatched; no
real DB or subprocesses.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts import check_holdout_roll as check_guard
from scripts import deploy_guard, holdout_roll


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _raw_frame(start: str, periods: int) -> pd.DataFrame:
    """Raw candle frame: epoch-second timestamp_utc + OHLCV."""
    ts = pd.date_range(start, periods=periods, freq="15min", tz="UTC")
    rng = np.random.default_rng(7)
    price = 4400 + np.cumsum(rng.normal(0, 0.5, periods))
    return pd.DataFrame(
        {
            "timestamp_utc": (ts - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1),
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": rng.integers(10, 100, periods).astype(float),
        }
    )


def _metrics(n_trades=40, expectancy=1.0, win_rate=0.5, sharpe_ratio=0.4, total_pnl=100.0, max_drawdown=-50.0):
    return {
        "n_trades": n_trades,
        "expectancy": expectancy,
        "win_rate": win_rate,
        "profit_factor": 1.2,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": 0.3,
        "total_pnl": total_pnl,
        "max_drawdown": max_drawdown,
    }


def _cfg_hr(model_path: str) -> dict:
    """Config for propose tests: small walk-forward windows so the ~90-day fake
    frame actually yields released test windows within [old, new)."""
    return {
        "holdout_roll": {
            "baseline_start": "2026-08-08",
            "cadence_days": 30,
            "step_days": 14,
            "primary_metric": "expectancy",
            "fallback_metrics": ["sharpe_ratio", "win_rate", "total_pnl"],
            "tolerance": 0.0,
            "released_min_trades": 20,
        },
        "validation": {"locked_holdout": {"enabled": True, "start": "2026-08-08", "end": None}},
        "general": {"db_path": "data/market_data_mt5.sqlite"},
        "market_data": {"timeframe": "M15"},
        "backtest": {"walk_forward": {"train_window_days": 5, "test_window_days": 3, "step_days": 2}},
        "assets": {"XAUUSD": {"enabled": True, "timeframe": "M15", "model_path": model_path}},
    }


def _ci_cfg(lock_start: str) -> dict:
    return {
        "holdout_roll": {"baseline_start": "2026-08-08"},
        "validation": {"locked_holdout": {"enabled": True, "start": lock_start, "end": None}},
    }


class _StubPredictor:
    def predict_proba(self, df):
        n = len(df)
        return {"p_long": np.full(n, 0.5), "p_short": np.full(n, 0.5)}


# --------------------------------------------------------------------------- #
# released_windows - pure slice filter (R4 part 2)
# --------------------------------------------------------------------------- #
def test_released_windows_filters_to_slice():
    from backtest.walk_forward import WalkForwardWindow

    ws = [
        WalkForwardWindow(1, 2, 100, 200),  # inside [100, 200)
        WalkForwardWindow(1, 2, 50, 99),  # before old_start
        WalkForwardWindow(1, 2, 200, 300),  # at/after new_start (test_start 200 >= 200)
        WalkForwardWindow(1, 2, 150, 250),  # crosses new_start
    ]
    rel = holdout_roll.released_windows(ws, old_start_ts=100, new_start_ts=200)
    assert [(w.test_start_ts, w.test_end_ts) for w in rel] == [(100, 200)]


def test_released_windows_empty_when_no_overlap():
    from backtest.walk_forward import WalkForwardWindow

    ws = [WalkForwardWindow(1, 2, 500, 600)]
    assert holdout_roll.released_windows(ws, 100, 200) == []


# --------------------------------------------------------------------------- #
# propose - no-peeking (R4)
# --------------------------------------------------------------------------- #
def test_propose_no_peeking(monkeypatch, tmp_path):
    """Hard proof: candidate training never reads data >= new_start, and the
    gate never scores outside [old_start, new_start)."""
    raw = _raw_frame("2026-07-01", 9000)  # ~93 days of 15-min bars
    monkeypatch.setattr(holdout_roll, "read_candles", lambda *a, **k: raw.copy())
    monkeypatch.setattr(holdout_roll, "build_full_df", lambda df, *a, **k: df.copy())
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", str(tmp_path / "j.csv"))

    train_max_ts: list[int] = []
    score_bounds: list[tuple[int, int]] = []

    def spy_fold(train_df, cfg, asset_key):
        train_max_ts.append(int(pd.to_numeric(train_df["timestamp_utc"]).max()))
        return _StubPredictor()

    def spy_score(test_df, cfg, asset_key, predictor):
        ts = pd.to_numeric(test_df["timestamp_utc"])
        score_bounds.append((int(ts.min()), int(ts.max())))
        return _metrics()

    monkeypatch.setattr(deploy_guard, "_candidate_fold_predictor", spy_fold)
    monkeypatch.setattr(deploy_guard, "_score_window", spy_score)
    monkeypatch.setattr(deploy_guard, "ModelPredictor", lambda *a, **k: _StubPredictor())

    mp = tmp_path / "XAUUSD.joblib"
    mp.write_bytes(b"x")  # exists so the incumbent is also scored
    cfg = _cfg_hr(str(mp))
    old, new = "2026-08-08", "2026-08-22"
    res = holdout_roll.propose(old, new, cfg=cfg, asset_key="XAUUSD", actor="tester")

    old_ts = holdout_roll._to_ts(old)
    new_ts = holdout_roll._to_ts(new)

    # R4 part 1: candidate training never uses data at/after new_start.
    assert train_max_ts, "candidate folds must be trained"
    assert all(t < new_ts for t in train_max_ts), "candidate trained on data at/after new_start"

    # R4 part 2: scoring only inside [old_start, new_start).
    assert score_bounds, "something must be scored"
    for lo, hi in score_bounds:
        assert lo >= old_ts, "scored before old_start"
        assert hi < new_ts, "scored at/after new_start"

    # The gate recorded a PROPOSED journal row.
    rows = holdout_roll.read_journal()
    assert len(rows) == 1
    assert rows[0]["decision"] == "PROPOSED"
    assert rows[0]["old_start"] == old and rows[0]["new_start"] == new
    assert rows[0]["actor"] == "tester"
    assert res["gate_pass"] is True


def test_propose_rejects_new_start_not_after_old(monkeypatch, tmp_path):
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", str(tmp_path / "j.csv"))
    with pytest.raises(ValueError):
        holdout_roll.propose("2026-08-22", "2026-08-08", cfg=_cfg_hr("/tmp/x"), asset_key="XAUUSD")


# --------------------------------------------------------------------------- #
# journal - move / rollback
# --------------------------------------------------------------------------- #
def test_move_lock_appends_moved_row(monkeypatch, tmp_path):
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", str(tmp_path / "j.csv"))
    out = holdout_roll.move_lock("2026-08-08", "2026-08-22", actor="owner")
    assert out["decision"] == "MOVED"
    rows = holdout_roll.read_journal()
    assert len(rows) == 1
    assert rows[0]["decision"] == "MOVED"
    assert rows[0]["new_start"] == "2026-08-22"
    assert rows[0]["old_start"] == "2026-08-08"


def test_rollback_appends_rolled_back_row(monkeypatch, tmp_path):
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", str(tmp_path / "j.csv"))
    holdout_roll.move_lock("2026-08-08", "2026-08-22", actor="owner")
    out = holdout_roll.rollback("2026-08-22", revert_to="2026-08-08", actor="owner")
    assert out["decision"] == "ROLLED_BACK"
    assert out["rollback_of"] == "2026-08-22"
    assert out["revert_to"] == "2026-08-08"
    rows = holdout_roll.read_journal()
    assert len(rows) == 2
    assert rows[1]["decision"] == "ROLLED_BACK"
    assert rows[1]["old_start"] == "2026-08-22"
    assert rows[1]["new_start"] == "2026-08-08"


def test_rollback_looks_up_predecessor(monkeypatch, tmp_path):
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", str(tmp_path / "j.csv"))
    holdout_roll.move_lock("2026-08-08", "2026-08-22", actor="owner")
    out = holdout_roll.rollback("2026-08-22", actor="owner")
    assert out["revert_to"] == "2026-08-08"


def test_rollback_without_predecessor_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", str(tmp_path / "j.csv"))
    with pytest.raises(ValueError):
        holdout_roll.rollback("2026-08-22", actor="owner")


# --------------------------------------------------------------------------- #
# current_lock_start + CI guard consistency
# --------------------------------------------------------------------------- #
def test_current_lock_start():
    assert holdout_roll.current_lock_start(_ci_cfg("2026-08-08")) == "2026-08-08"
    assert holdout_roll.current_lock_start({"validation": {}}) is None


def test_expected_lock_start_helper():
    cfg = _ci_cfg("whatever")
    assert check_guard.expected_lock_start(cfg, []) == "2026-08-08"
    assert (
        check_guard.expected_lock_start(cfg, [{"decision": "MOVED", "new_start": "2026-09-01"}])
        == "2026-09-01"
    )
    assert (
        check_guard.expected_lock_start(
            cfg, [{"decision": "PROPOSED", "old_start": "2026-08-08", "new_start": "2026-09-01"}]
        )
        == "2026-08-08"
    )
    assert (
        check_guard.expected_lock_start(
            cfg,
            [
                {"decision": "MOVED", "new_start": "2026-09-01"},
                {"decision": "ROLLED_BACK", "new_start": "2026-08-08", "rollback_of": "2026-09-01"},
            ],
        )
        == "2026-08-08"
    )


def test_ci_consistent_when_empty_and_baseline():
    ok, _ = check_guard.check_consistency(_ci_cfg("2026-08-08"), rows=[])
    assert ok


def test_ci_drift_when_empty_but_not_baseline():
    ok, _ = check_guard.check_consistency(_ci_cfg("2026-08-22"), rows=[])
    assert not ok


def test_ci_consistent_after_moved():
    ok, _ = check_guard.check_consistency(
        _ci_cfg("2026-08-22"),
        rows=[{"decision": "MOVED", "old_start": "2026-08-08", "new_start": "2026-08-22"}],
    )
    assert ok


def test_ci_drift_after_moved_but_config_not_updated():
    ok, _ = check_guard.check_consistency(
        _ci_cfg("2026-08-08"),
        rows=[{"decision": "MOVED", "old_start": "2026-08-08", "new_start": "2026-08-22"}],
    )
    assert not ok


def test_ci_consistent_while_proposed_pending():
    ok, _ = check_guard.check_consistency(
        _ci_cfg("2026-08-08"),
        rows=[{"decision": "PROPOSED", "old_start": "2026-08-08", "new_start": "2026-08-22"}],
    )
    assert ok


def test_ci_drift_when_proposed_but_config_already_moved():
    ok, _ = check_guard.check_consistency(
        _ci_cfg("2026-08-22"),
        rows=[{"decision": "PROPOSED", "old_start": "2026-08-08", "new_start": "2026-08-22"}],
    )
    assert not ok


def test_ci_consistent_after_rolled_back():
    ok, _ = check_guard.check_consistency(
        _ci_cfg("2026-08-08"),
        rows=[
            {"decision": "MOVED", "old_start": "2026-08-08", "new_start": "2026-08-22"},
            {
                "decision": "ROLLED_BACK",
                "old_start": "2026-08-22",
                "new_start": "2026-08-08",
                "rollback_of": "2026-08-22",
            },
        ],
    )
    assert ok


# --------------------------------------------------------------------------- #
# CI matrix - git tracking of the journal (micro-fix: logs/*.csv is gitignored,
# so the journal must be force-added to the move/rollback commit or CI will not
# see it after checkout and falsely fail).
# --------------------------------------------------------------------------- #
def _write_journal(path: str, rows: list[dict]) -> None:
    import csv

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=holdout_roll.JOURNAL_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in holdout_roll.JOURNAL_COLUMNS})


def test_ci_guard_committed_journal_moved_is_consistent(monkeypatch, tmp_path):
    """Journal force-added/committed with the last MOVED row; config matches
    the move -> CI green (exit 0). Reads the journal from a real file, as CI
    would after checkout."""
    jpath = str(tmp_path / "holdout_roll_journal.csv")
    _write_journal(
        jpath,
        [
            {
                "ts": "2026-08-30T00:00:00Z",
                "old_start": "2026-08-08",
                "new_start": "2026-08-22",
                "gate_metrics_json": "",
                "decision": "MOVED",
                "actor": "owner",
                "rollback_of": "",
            }
        ],
    )
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", jpath)
    # rows=None -> read the committed journal file (not the in-memory list).
    ok, _ = check_guard.check_consistency(_ci_cfg("2026-08-22"), rows=None)
    assert ok


def test_ci_guard_config_shifted_without_journal_fails(monkeypatch, tmp_path):
    """Reproduces the bug this micro-fix prevents: the lock was shifted in
    config (2026-08-08 -> 2026-08-22) but the journal was NOT force-added, so
    after checkout CI sees an empty journal -> expects baseline -> drift -> fail
    (exit 1)."""
    jpath = str(tmp_path / "holdout_roll_journal.csv")  # not created
    monkeypatch.setattr(holdout_roll, "JOURNAL_PATH", jpath)
    ok, msg = check_guard.check_consistency(_ci_cfg("2026-08-22"), rows=None)
    assert not ok
    assert "baseline" in msg
