"""Tests for model/registry.py (ТЗ 8.4 Model Registry, Phase 2 Step 7).

Covers: registration with hash + fingerprint cross-check, atomic activation,
rollback, integrity verification (corruption detection), history ordering,
and the pipeline integrations (train_all_assets registers non-fatally;
deploy_guard blocks unregistered / corrupted candidates).
"""

import os
import sys

import joblib
import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from model.registry import (
    FingerprintMismatchError,
    ModelRegistry,
    RegistryError,
    RegistryIntegrityError,
    file_sha256,
    register_trained_model,
)
from model.trainer import compute_model_fingerprint, save_model


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
class _PicklableModel:
    """Tiny picklable stand-in with a stable repr (fingerprint fallback)."""

    def __init__(self, p_long: float = 0.6):
        self.p_long = float(p_long)
        self.classes_ = np.array([0, 1])

    def __repr__(self):  # deterministic -> stable content fingerprint
        return f"_PicklableModel(p_long={self.p_long!r})"


def _make_bundle(path, p_long=0.6, metadata=None):
    """Save a joblib bundle the way save_model does (with self-hash)."""
    model = _PicklableModel(p_long)
    cols = ["f"]
    save_model(model, cols, str(path), metadata=dict(metadata or {}))
    return path


@pytest.fixture
def registry(tmp_path):
    return ModelRegistry(tmp_path / "registry")


@pytest.fixture
def model_file(tmp_path):
    p = tmp_path / "models" / "XAUUSD.joblib"
    p.parent.mkdir(parents=True, exist_ok=True)
    _make_bundle(p)
    return p


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #
def test_register_creates_entry_with_hash(registry, model_file):
    rid = registry.register(model_file, "XAUUSD", "M5", trained_at_utc_ms=1000)
    entry = registry.get(rid)
    assert entry is not None
    assert entry.asset == "XAUUSD"
    assert entry.timeframe == "M5"
    assert entry.file_sha256 == file_sha256(model_file)
    assert entry.fingerprint is not None  # recomputed from the bundle
    assert entry.model_path == str(model_file)
    # index.jsonl written next to the pointer, physical file NOT moved
    assert (registry.root / "index.jsonl").exists()
    assert model_file.exists()


def test_register_verifies_fingerprint_match(registry, model_file):
    # Declared fingerprint equal to the recomputed one -> registers fine.
    fp = compute_model_fingerprint(*_load_bundle_parts(model_file))
    rid = registry.register(model_file, "XAUUSD", "M5", fingerprint=fp)
    assert registry.get(rid).fingerprint == fp

    # A WRONG declared fingerprint is refused (identity cannot be faked).
    with pytest.raises(FingerprintMismatchError):
        registry.register(model_file, "XAUUSD", "M5", fingerprint="0" * 64)


def _load_bundle_parts(path):
    bundle = joblib.load(str(path))
    return bundle["model"], bundle["feature_cols"]


def test_register_trained_model_reads_metadata(tmp_path, registry):
    """register_trained_model passes the stored self-hash as the declared
    fingerprint: a bundle corrupted after training is refused."""
    p = tmp_path / "b" / "EURUSD.joblib"
    p.parent.mkdir(parents=True)
    save_model(
        _PicklableModel(0.7),
        ["f"],
        str(p),
        metadata={"model_hash": "0" * 64, "trained_at_utc": "2026-01-01T00:00:00+00:00"},
    )
    # Stored self-hash does not match the actual content -> mismatch.
    with pytest.raises(FingerprintMismatchError):
        register_trained_model(p, "EURUSD", "M15", root=registry.root)

    # A proper bundle (self-hash written by save_model) registers cleanly.
    p2 = tmp_path / "b2" / "EURUSD.joblib"
    p2.parent.mkdir(parents=True)
    save_model(_PicklableModel(0.7), ["f"], str(p2), metadata={"trained_at_utc": "2026-01-01T00:00:00+00:00"})
    rid = register_trained_model(p2, "EURUSD", "M15", root=registry.root)
    entry = registry.get(rid)
    assert entry.trained_at_utc_ms == 1767225600000  # 2026-01-01T00:00:00Z


# --------------------------------------------------------------------------- #
# activate / get_active / rollback
# --------------------------------------------------------------------------- #
def test_activate_and_get_active(registry, tmp_path):
    p1 = tmp_path / "m1.joblib"
    p2 = tmp_path / "m2.joblib"
    _make_bundle(p1, p_long=0.6)
    _make_bundle(p2, p_long=0.7)
    rid1 = registry.register(p1, "XAUUSD", "M5", registered_at_utc_ms=1000)
    rid2 = registry.register(p2, "XAUUSD", "M5", registered_at_utc_ms=2000)

    assert registry.get_active("XAUUSD", "M5") is None
    registry.activate(rid2)
    assert registry.get_active("XAUUSD", "M5").registry_id == rid2
    registry.activate(rid1)
    assert registry.get_active("XAUUSD", "m5").registry_id == rid1  # case-insensitive
    # Other asset+tf pointers unaffected.
    assert registry.get_active("GBPUSD", "M5") is None


def test_rollback_to_previous(registry, tmp_path):
    paths = []
    rids = []
    for i, pl in enumerate((0.5, 0.6, 0.7)):
        p = tmp_path / f"m{i}.joblib"
        _make_bundle(p, p_long=pl)
        rids.append(registry.register(p, "BTCUSD", "M15", registered_at_utc_ms=1000 + i))
        paths.append(p)

    registry.activate(rids[2])
    assert registry.get_active("BTCUSD", "M15").registry_id == rids[2]

    # Plain rollback -> previous entry.
    prev = registry.rollback("BTCUSD", "M15")
    assert prev.registry_id == rids[1]
    assert registry.get_active("BTCUSD", "M15").registry_id == rids[1]

    # Explicit target rollback.
    target = registry.rollback("BTCUSD", "M15", to_registry_id=rids[0])
    assert target.registry_id == rids[0]

    # Foreign registry_id for this asset+tf is refused.
    other = tmp_path / "other.joblib"
    _make_bundle(other, p_long=0.4)
    other_rid = registry.register(other, "ETHUSD", "M15", registered_at_utc_ms=1)
    with pytest.raises(RegistryError):
        registry.rollback("BTCUSD", "M15", to_registry_id=other_rid)


def test_history_ordering(registry, tmp_path):
    # Register out of chronological order; history must still be sorted oldest-first.
    for ts, pl in ((3000, 0.7), (1000, 0.5), (2000, 0.6)):
        p = tmp_path / f"h{ts}.joblib"
        _make_bundle(p, p_long=pl)
        registry.register(p, "XAGUSD", "M15", registered_at_utc_ms=ts)

    hist = registry.history("XAGUSD", "M15")
    assert [e.registered_at_utc_ms for e in hist] == [1000, 2000, 3000]
    # list_entries filters by asset.
    assert all(e.asset == "XAGUSD" for e in registry.list_entries("xagusd"))
    assert len(registry.list_entries()) == 3


# --------------------------------------------------------------------------- #
# integrity / atomicity
# --------------------------------------------------------------------------- #
def test_verify_detects_corrupted_model(registry, model_file):
    rid = registry.register(model_file, "XAUUSD", "M5")
    assert registry.verify(rid) is True
    assert registry.verify("no-such-id") is False

    # Corrupt the file content -> verify fails.
    data = bytearray(model_file.read_bytes())
    data[len(data) // 2] ^= 0xFF
    model_file.write_bytes(bytes(data))
    assert registry.verify(rid) is False


def test_atomic_activation(registry, tmp_path):
    """A damaged artifact must never become active; the existing pointer is
    preserved (tmp+replace pointer, integrity-checked activation)."""
    good = tmp_path / "good.joblib"
    bad = tmp_path / "bad.joblib"
    _make_bundle(good, p_long=0.6)
    _make_bundle(bad, p_long=0.7)
    rid_good = registry.register(good, "GBPUSD", "M15", registered_at_utc_ms=1000)
    rid_bad = registry.register(bad, "GBPUSD", "M15", registered_at_utc_ms=2000)

    registry.activate(rid_good)
    # Corrupt the second artifact after registration.
    bad.write_bytes(b"garbage-not-a-model")

    with pytest.raises(RegistryIntegrityError):
        registry.activate(rid_bad)
    # Pointer unchanged and no tmp leftovers.
    assert registry.get_active("GBPUSD", "M15").registry_id == rid_good
    leftovers = [p.name for p in registry.root.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []

    # Corrupting the ACTIVE model also blocks re-activation.
    good.write_bytes(b"garbage-not-a-model")
    with pytest.raises(RegistryIntegrityError):
        registry.activate(rid_good)
    with pytest.raises(RegistryError):
        registry.register(tmp_path / "missing.joblib", "GBPUSD", "M15")


# --------------------------------------------------------------------------- #
# Pipeline integrations
# --------------------------------------------------------------------------- #
def test_train_all_assets_registers(tmp_path, monkeypatch, capsys):
    """Integration: after each successful (mocked) training the artifact is
    registered; a registry failure is a warning, never an abort."""
    from scripts import train_all_assets as taa

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    models = {}
    for asset in ("XAUUSD", "GBPUSD"):
        p = model_dir / f"{asset}.joblib"
        _make_bundle(p)
        models[asset] = str(p)

    cfg = {
        "retraining": {"enabled": True},
        "general": {"db_path": str(tmp_path / "db.sqlite")},
        "market_data": {"timeframe": "M5"},
        "assets": {
            "XAUUSD": {"enabled": True, "mt5_symbol": "XAUUSD", "model_path": models["XAUUSD"]},
            "GBPUSD": {"enabled": True, "mt5_symbol": "GBPUSD", "model_path": models["GBPUSD"]},
        },
    }
    monkeypatch.setattr(taa, "load_config", lambda: cfg)

    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)
        return None

    monkeypatch.setattr(taa.subprocess, "run", fake_run)
    monkeypatch.setattr(taa, "default_registry_root", lambda: tmp_path / "registry", raising=False)
    # train_all_assets uses register_trained_model -> ModelRegistry(root=None)
    # so route it to the tmp registry via the env-style default override:
    import model.registry as mr

    monkeypatch.setattr(mr, "default_registry_root", lambda: tmp_path / "registry")

    taa.main()

    reg = ModelRegistry(tmp_path / "registry")
    assert len(calls) == 2  # training flow untouched (one subprocess per asset)
    assert len(reg.list_entries()) == 2
    out = capsys.readouterr().out
    assert "registry: registered" in out

    # Non-fatal: a registry blow-up mid-pipeline does not abort training.
    def boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(taa, "register_in_registry", boom)
    taa.main()
    out2 = capsys.readouterr().out
    assert "registry WARNING" in out2
    assert "ALL MULTI-ASSET MODELS TRAINED SUCCESSFULLY" in out2


def test_deploy_guard_blocks_unregistered(tmp_path, monkeypatch):
    """deploy_guard: an unregistered deploy candidate is blocked with a clear
    error, and a hash-corrupted registered one too (fail-closed pre-flight)."""
    from scripts import deploy_guard as dg

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    mp = model_dir / "XAUUSD.joblib"
    _make_bundle(mp)
    bak = model_dir / "XAUUSD.joblib.deploy_guard.bak"
    _make_bundle(bak, p_long=0.55)

    reg = ModelRegistry(tmp_path / "registry")
    cfg = {
        "deploy_guard": {
            "enabled": True,
            "primary_metric": "expectancy",
            "fallback_metrics": ["sharpe_ratio", "win_rate", "total_pnl"],
            "min_trades": 20,
            "tolerance": 0.0,
            "backup_suffix": ".deploy_guard.bak",
        },
        "general": {"db_path": str(tmp_path / "db.sqlite")},
        "market_data": {"timeframe": "M5"},
        "backtest": {"walk_forward": {"train_window_days": 300, "test_window_days": 50, "step_days": 50}},
        "assets": {"XAUUSD": {"enabled": True, "model_path": str(mp)}},
    }
    monkeypatch.setattr(
        dg,
        "guard_asset",
        lambda cfg, asset, path: {
            "asset": asset,
            "deploy": True,
            "metric": None,
            "deployed_value": None,
            "candidate_value": None,
            "reason": "ok",
            "tolerance": 0.0,
            "min_trades": 20,
        },
    )

    # 1. Unregistered candidate -> the deploy is blocked (rolled back from bak).
    decisions, failed = dg.validate_and_deploy(cfg, registry=reg)
    assert failed is True
    dec = decisions[0]
    assert dec["deploy"] is False
    assert "model_not_registered" in dec["reason"]
    assert dec.get("rolled_back") is True

    def restore_bak():
        # Re-create the nightly backup (the first pass removed it).
        _make_bundle(bak, p_long=0.55)

    # 2. Registered candidate passes the pre-flight (heavy guard is mocked OK).
    rid = reg.register(mp, "XAUUSD", "M5")
    restore_bak()
    decisions, failed = dg.validate_and_deploy(cfg, registry=reg)
    assert failed is False
    assert decisions[0]["deploy"] is True
    assert decisions[0].get("registry_id") == rid

    # 3. Registered but hash-corrupted -> blocked with model_corrupted
    #    (path matches the entry, content hash does not).
    data = bytearray(mp.read_bytes())
    data[0] ^= 0xFF
    mp.write_bytes(bytes(data))
    restore_bak()
    decisions, failed = dg.validate_and_deploy(cfg, registry=reg)
    assert failed is True
    assert "model_corrupted" in decisions[0]["reason"]
    assert decisions[0].get("rolled_back") is True


def test_deploy_guard_registry_preflight_unit(tmp_path):
    """Unit level: registry_preflight_check reason strings."""
    from scripts import deploy_guard as dg

    mp = tmp_path / "m.joblib"
    _make_bundle(mp)
    reg = ModelRegistry(tmp_path / "reg")

    # Missing file -> ok (nothing to deploy; other guards handle it).
    assert dg.registry_preflight_check({}, "XAUUSD", str(tmp_path / "nope.joblib"), registry=reg)["ok"] is True
    # Unregistered -> blocked.
    pre = dg.registry_preflight_check({}, "XAUUSD", str(mp), registry=reg)
    assert pre["ok"] is False and "model_not_registered" in pre["reason"]
    # Registered + intact -> ok.
    rid = reg.register(mp, "XAUUSD", "M5")
    pre = dg.registry_preflight_check({}, "XAUUSD", str(mp), registry=reg)
    assert pre["ok"] is True and pre["registry_id"] == rid
    # Corrupted -> blocked.
    mp.write_bytes(b"junk")
    pre = dg.registry_preflight_check({}, "XAUUSD", str(mp), registry=reg)
    assert pre["ok"] is False and "model_corrupted" in pre["reason"]
