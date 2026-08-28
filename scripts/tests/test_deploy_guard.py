"""
Step 7 regression tests for Part B Phase 6 / audit #25 (deploy guard).

The nightly retrain (scripts/overnight.py stages 3b/4b) must never silently
overwrite a good production model with a bad one. The deploy guard:
  * --backup  snapshots each enabled asset's production model to a sidecar
              backup BEFORE the retrain overwrites it (idempotent).
  * --check   walk-forward validates the freshly retrained model against the
              backed-up incumbent on the SAME out-of-sample windows; if the
              candidate regressed beyond `tolerance` (or has too little OOS
              evidence, per `min_trades`), the incumbent is restored and the
              stage exits 1 -> overnight reports FAILED (Telegram ❌).

Tests are pure unit tests: the decision logic (`is_improvement`,
`decide_from_evaluations`, `aggregate_fold_metrics`) is tested with plain
dicts, and the orchestration (`backup_production_models`, `validate_and_deploy`,
`main`) is tested with tmp files and monkeypatched heavy dependencies - no
subprocesses or a real SQLite DB.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts import deploy_guard as dg


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _metrics(
    n_trades=40,
    expectancy=1.0,
    win_rate=0.5,
    sharpe_ratio=0.4,
    total_pnl=100.0,
    max_drawdown=-50.0,
):
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


def _cfg(tmp_path, model_path=None, dg_overrides=None):
    if model_path is None:
        model_path = str(tmp_path / "models" / "XAUUSD.joblib")
    cfg = {
        "deploy_guard": {
            "enabled": True,
            "primary_metric": "expectancy",
            "fallback_metrics": ["sharpe_ratio", "win_rate", "total_pnl"],
            "min_trades": 20,
            "tolerance": 0.0,
            "backup_suffix": ".deploy_guard.bak",
        },
        "general": {"db_path": str(tmp_path / "market.sqlite")},
        "market_data": {"timeframe": "M5"},
        "backtest": {
            "walk_forward": {
                "train_window_days": 300,
                "test_window_days": 50,
                "step_days": 50,
            }
        },
        "assets": {
            "XAUUSD": {"enabled": True, "model_path": model_path},
            "DISABLED": {"enabled": False, "model_path": str(tmp_path / "nope.joblib")},
        },
    }
    if dg_overrides:
        cfg["deploy_guard"].update(dg_overrides)
    return cfg


# --------------------------------------------------------------------------- #
# is_improvement - pure decision rule (conservative, no-look-ahead contract)
# --------------------------------------------------------------------------- #
def test_improvement_deploys_equal_value():
    dec = dg.is_improvement(_metrics(expectancy=1.0), _metrics(expectancy=1.0))
    assert dec["deploy"] is True
    assert dec["reason"] == "ok"
    assert dec["metric"] == "expectancy"


def test_candidate_better_deploys():
    dec = dg.is_improvement(_metrics(expectancy=1.0), _metrics(expectancy=2.5))
    assert dec["deploy"] is True and dec["reason"] == "ok"


def test_regression_beyond_tolerance_is_rejected():
    dec = dg.is_improvement(_metrics(expectancy=2.0), _metrics(expectancy=1.0), tolerance=0.0)
    assert dec["deploy"] is False
    assert dec["reason"] == "regressed_beyond_tolerance"


def test_small_regression_within_tolerance_is_allowed():
    dec = dg.is_improvement(_metrics(expectancy=2.0), _metrics(expectancy=1.9), tolerance=0.2)
    assert dec["deploy"] is True
    assert dec["reason"] == "within_tolerance"


def test_candidate_too_little_evidence_never_replaces_proven_model():
    strong = _metrics(expectancy=1.5, n_trades=60)
    weak = _metrics(expectancy=99.0, n_trades=5)  # looks great but thin evidence
    dec = dg.is_improvement(strong, weak, min_trades=20)
    assert dec["deploy"] is False
    assert dec["reason"] == "candidate_too_little_evidence"


def test_candidate_with_evidence_replaces_thin_incumbent():
    thin_incumbent = _metrics(expectancy=1.5, n_trades=3)
    proven_candidate = _metrics(expectancy=1.2, n_trades=50)
    dec = dg.is_improvement(thin_incumbent, proven_candidate, min_trades=20)
    assert dec["deploy"] is True
    assert dec["reason"] == "candidate_has_evidence_incumbent_thin"


def test_fallback_chain_used_when_primary_missing():
    deployed = _metrics(expectancy=None, sharpe_ratio=0.5)
    candidate = _metrics(expectancy=None, sharpe_ratio=1.0)
    dec = dg.is_improvement(deployed, candidate, primary="expectancy")
    assert dec["deploy"] is True
    assert dec["metric"] == "sharpe_ratio"


def test_missing_metric_on_either_side_walks_fallback_chain():
    # expectancy missing on candidate, sharpe_ratio missing on both -> the
    # chain walks to win_rate (the first metric both sides carry a value for).
    deployed = _metrics(expectancy=1.0, sharpe_ratio=None, win_rate=0.4)
    candidate = _metrics(expectancy=None, sharpe_ratio=None, win_rate=0.55)
    dec = dg.is_improvement(deployed, candidate)
    assert dec["metric"] == "win_rate"
    assert dec["deploy"] is True


def test_both_thin_still_compared_but_flagged():
    # Both sides have < min_trades: neither the "candidate thin" nor the
    # "incumbent thin" branch fires; a regression is still rejected.
    dec = dg.is_improvement(
        _metrics(expectancy=2.0, n_trades=5),
        _metrics(expectancy=0.5, n_trades=8),
        min_trades=20,
    )
    assert dec["deploy"] is False
    assert dec["reason"] == "regressed_beyond_tolerance"


# --------------------------------------------------------------------------- #
# aggregate_fold_metrics
# --------------------------------------------------------------------------- #
def test_aggregate_fold_metrics_averages_scores_sums_pnl_min_drawdown():
    f1 = _metrics(n_trades=10, expectancy=1.0, win_rate=0.5, total_pnl=100.0, max_drawdown=-50.0)
    f2 = _metrics(n_trades=20, expectancy=2.0, win_rate=0.6, total_pnl=300.0, max_drawdown=-80.0)
    agg = dg.aggregate_fold_metrics([f1, f2])
    assert agg["n_folds"] == 2
    assert agg["n_trades"] == 30
    assert agg["expectancy"] == pytest.approx(1.5)
    assert agg["win_rate"] == pytest.approx(0.55)
    assert agg["total_pnl"] == pytest.approx(400.0)
    assert agg["max_drawdown"] == pytest.approx(-80.0)


def test_aggregate_fold_metrics_zero_folds_is_all_none():
    agg = dg.aggregate_fold_metrics([])
    assert agg["n_folds"] == 0
    assert agg["n_trades"] == 0
    assert agg["expectancy"] is None
    assert agg["total_pnl"] is None


def test_aggregate_fold_metrics_skips_nan_values():
    f1 = _metrics(expectancy=float("nan"), total_pnl=float("nan"))
    f2 = _metrics(expectancy=3.0, total_pnl=50.0)
    agg = dg.aggregate_fold_metrics([f1, f2])
    assert agg["expectancy"] == pytest.approx(3.0)
    assert agg["total_pnl"] == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# decide_from_evaluations - pure wrapper around the decision
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cfg_dict():
    return {
        "deploy_guard": {
            "enabled": True,
            "primary_metric": "expectancy",
            "fallback_metrics": ["sharpe_ratio", "win_rate", "total_pnl"],
            "min_trades": 20,
            "tolerance": 0.0,
        }
    }


def test_decide_no_valid_windows_deploys_only_without_incumbent(cfg_dict):
    # No incumbent -> first deploy allowed even without windows.
    dec = dg.decide_from_evaluations(None, None, cfg_dict, has_incumbent=False, windows_valid=False)
    assert dec["deploy"] is True and dec["reason"] == "no_valid_windows"
    # Has incumbent -> never overwrite without valid evidence.
    dec = dg.decide_from_evaluations(None, None, cfg_dict, has_incumbent=True, windows_valid=False)
    assert dec["deploy"] is False and dec["reason"] == "no_valid_windows"


def test_decide_no_deployed_model_deploys(cfg_dict):
    dec = dg.decide_from_evaluations(None, _metrics(), cfg_dict, has_incumbent=False, windows_valid=True)
    assert dec["deploy"] is True and dec["reason"] == "no_deployed_model"


def test_decide_candidate_evaluation_failure_rejects(cfg_dict):
    dec = dg.decide_from_evaluations(_metrics(), None, cfg_dict, has_incumbent=True, windows_valid=True)
    assert dec["deploy"] is False and dec["reason"] == "candidate_evaluation_failed"


def test_decide_propagates_improvement_decision(cfg_dict):
    dec = dg.decide_from_evaluations(
        _metrics(expectancy=2.0),
        _metrics(expectancy=0.5),
        cfg_dict,
        has_incumbent=True,
        windows_valid=True,
    )
    assert dec["deploy"] is False and dec["reason"] == "regressed_beyond_tolerance"


def test_decide_reads_tolerance_from_cfg(cfg_dict):
    cfg_dict["deploy_guard"]["tolerance"] = 2.0
    dec = dg.decide_from_evaluations(
        _metrics(expectancy=2.0),
        _metrics(expectancy=0.5),
        cfg_dict,
        has_incumbent=True,
        windows_valid=True,
    )
    assert dec["deploy"] is True and dec["reason"] == "within_tolerance"


# --------------------------------------------------------------------------- #
# enabled_assets
# --------------------------------------------------------------------------- #
def test_enabled_assets_only_returns_enabled(tmp_path):
    assets = dg.enabled_assets(_cfg(tmp_path))
    assert assets == ["XAUUSD"]


# --------------------------------------------------------------------------- #
# backup_production_models (--backup)
# --------------------------------------------------------------------------- #
def test_backup_no_model_is_noted(tmp_path):
    cfg = _cfg(tmp_path, model_path=str(tmp_path / "missing.joblib"))
    results = dg.backup_production_models(cfg)
    assert ("XAUUSD", "no_model", True) in results


def test_backup_creates_sidecar(tmp_path):
    mp = tmp_path / "XAUUSD.joblib"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"good-model")
    cfg = _cfg(tmp_path, model_path=str(mp))
    results = dg.backup_production_models(cfg)
    assert ("XAUUSD", "backed_up", True) in results
    assert (mp.parent / "XAUUSD.joblib.deploy_guard.bak").read_bytes() == b"good-model"


def test_backup_is_idempotent_and_never_overwrites_existing(tmp_path):
    mp = tmp_path / "XAUUSD.joblib"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"good-model")
    cfg = _cfg(tmp_path, model_path=str(mp))
    dg.backup_production_models(cfg)
    # First refresh would produce a different model; the existing backup must win.
    mp.write_bytes(b"new-model")
    results = dg.backup_production_models(cfg)
    assert ("XAUUSD", "already_backed_up", True) in results
    bak = mp.parent / "XAUUSD.joblib.deploy_guard.bak"
    assert bak.read_bytes() == b"good-model"


# --------------------------------------------------------------------------- #
# validate_and_deploy (--check) - rollback semantics
# --------------------------------------------------------------------------- #
def test_rejected_asset_is_rolled_back_and_backup_removed(tmp_path, monkeypatch):
    mp = tmp_path / "XAUUSD.joblib"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"new-nightly-model")
    bak = tmp_path / "XAUUSD.joblib.deploy_guard.bak"
    bak.write_bytes(b"good-incumbent")

    cfg = _cfg(tmp_path, model_path=str(mp))
    # Registry pre-flight mocked OK: this test covers the rollback mechanics,
    # not the registry (see model/tests/test_registry.py).
    monkeypatch.setattr(
        dg,
        "registry_preflight_check",
        lambda cfg_, asset, path, registry=None: {
            "ok": True,
            "reason": "registered_and_verified",
            "registry_id": "XAUUSD-M5-test",
        },
    )
    monkeypatch.setattr(
        dg,
        "guard_asset",
        staticmethod(lambda cfg_, asset, bak_: {"deploy": False, "reason": "regressed_beyond_tolerance"}),
    )
    decisions, failed = dg.validate_and_deploy(cfg)
    assert failed is True
    assert len(decisions) == 1
    dec = decisions[0]
    assert dec["deploy"] is False
    assert dec["rolled_back"] is True
    # The good model was restored over the production path.
    assert mp.read_bytes() == b"good-incumbent"
    # The backup sidecar is cleaned up in all terminal states.
    assert not os.path.exists(bak)


def test_ok_asset_keeps_new_model_and_removes_backup(tmp_path, monkeypatch):
    mp = tmp_path / "XAUUSD.joblib"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"new-nightly-model")
    bak = tmp_path / "XAUUSD.joblib.deploy_guard.bak"
    bak.write_bytes(b"good-incumbent")

    cfg = _cfg(tmp_path, model_path=str(mp))
    # Registry pre-flight (ТЗ 8.4) is mocked: this test covers the deploy
    # decision/backup mechanics, not the registry (see model/tests/test_registry.py).
    monkeypatch.setattr(
        dg,
        "registry_preflight_check",
        lambda cfg_, asset, path, registry=None: {
            "ok": True,
            "reason": "registered_and_verified",
            "registry_id": "XAUUSD-M5-test",
        },
    )
    monkeypatch.setattr(
        dg,
        "guard_asset",
        staticmethod(lambda cfg_, asset, bak_: {"deploy": True, "reason": "ok"}),
    )
    decisions, failed = dg.validate_and_deploy(cfg)
    assert failed is False
    assert decisions[0]["deploy"] is True
    assert decisions[0]["registry_id"] == "XAUUSD-M5-test"
    assert mp.read_bytes() == b"new-nightly-model"
    assert not os.path.exists(bak)


def test_no_backup_keeps_new_model_without_rollback(tmp_path, monkeypatch):
    mp = tmp_path / "XAUUSD.joblib"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"new-nightly-model")
    cfg = _cfg(tmp_path, model_path=str(mp))
    results = dg.validate_and_deploy(cfg)
    decisions, failed = results
    assert failed is False
    assert decisions[0]["deploy"] is True
    assert decisions[0]["reason"] == "no_backup_no_rollback"
    assert mp.read_bytes() == b"new-nightly-model"


def test_guard_error_rolls_back_and_marks_failed(tmp_path, monkeypatch):
    mp = tmp_path / "XAUUSD.joblib"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"new-nightly-model")
    bak = tmp_path / "XAUUSD.joblib.deploy_guard.bak"
    bak.write_bytes(b"good-incumbent")

    cfg = _cfg(tmp_path, model_path=str(mp))
    # Registry pre-flight mocked OK: this test exercises the generic error path.
    monkeypatch.setattr(
        dg,
        "registry_preflight_check",
        lambda cfg_, asset, path, registry=None: {
            "ok": True,
            "reason": "registered_and_verified",
            "registry_id": "XAUUSD-M5-test",
        },
    )
    monkeypatch.setattr(
        dg,
        "guard_asset",
        staticmethod(lambda cfg_, asset, bak_: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    decisions, failed = dg.validate_and_deploy(cfg)
    assert failed is True
    assert decisions[0]["reason"] == "error:boom"
    assert decisions[0]["rolled_back"] is True
    assert mp.read_bytes() == b"good-incumbent"
    assert not os.path.exists(bak)


# --------------------------------------------------------------------------- #
# main - CLI exit codes map to overnight stage status
# --------------------------------------------------------------------------- #
def test_main_disabled_returns_zero_even_for_check(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, dg_overrides={"enabled": False})
    monkeypatch.setattr(dg, "load_config", staticmethod(lambda: cfg))
    assert dg.main(["--check"]) == 0


def test_main_backup_returns_zero(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(dg, "load_config", staticmethod(lambda: cfg))
    monkeypatch.setattr(dg, "backup_production_models", staticmethod(lambda *a, **k: []))
    assert dg.main(["--backup"]) == 0


def test_main_check_ok_returns_zero(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(dg, "load_config", staticmethod(lambda: cfg))
    monkeypatch.setattr(dg, "validate_and_deploy", staticmethod(lambda *a, **k: ([], False)))
    # Patch weekend audit so it doesn't hit real CSVs
    import scripts.audit_weekend_tags as _awt

    monkeypatch.setattr(_awt, "audit_weekend_tags", lambda log_dir="logs": [])
    assert dg.main(["--check"]) == 0


def test_main_check_rejected_returns_one(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(dg, "load_config", staticmethod(lambda: cfg))
    monkeypatch.setattr(dg, "validate_and_deploy", staticmethod(lambda *a, **k: ([], True)))
    assert dg.main(["--check"]) == 1
