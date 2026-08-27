"""Model Registry (ТЗ 8.4): catalog of trained models with versioning,
activation and rollback.

Design contract (Step 7 / Phase 2):

* The registry CATALOGS models - it never moves or rewrites a physical model
  file. Each entry stores the model's absolute path, its sha256 file hash and
  (when available) the deterministic content fingerprint already produced by
  ``model.trainer.compute_model_fingerprint`` (the same value stored inside
  every production bundle as ``metadata.model_hash`` by ``save_model``).
  ``scripts/verify_model_fingerprints.py`` stays the audit surface for the
  files themselves; the registry reuses that fingerprint format instead of
  inventing a second one.
* Storage layout under the registry root (default ``models/registry/``,
  overridable via the ``MODEL_REGISTRY_ROOT`` environment variable):
      index.jsonl   - append-only log, one JSON object per registration
      active.json   - pointer {"ASSET|TIMEFRAME": registry_id}, written
                      atomically (tmp file + os.replace)
* Activation semantics: ``activate()`` refuses to point at a registry entry
  whose model file is missing or whose sha256 no longer matches the recorded
  hash (corrupted artifact) - the pointer is left untouched. The active
  pointer is metadata-only for now: ``ModelPredictor`` still loads models by
  the fixed config path, so wiring the pointer into inference is deferred to
  the next step (documented in plans/execution-plan.md). Activation already
  updates registry state consistently and atomically, so the later switch is
  a pure read-side change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("model_registry")

# Environment override so deployments can relocate the registry without code
# changes; unset -> models/registry/ relative to the project root.
ENV_REGISTRY_ROOT = "MODEL_REGISTRY_ROOT"
DEFAULT_REGISTRY_ROOT = Path("models") / "registry"

_INDEX_NAME = "index.jsonl"
_ACTIVE_NAME = "active.json"


def registry_root_from_env() -> Optional[Path]:
    """Return the registry root from ``MODEL_REGISTRY_ROOT`` or None."""
    raw = os.environ.get(ENV_REGISTRY_ROOT)
    return Path(raw) if raw else None


def default_registry_root() -> Path:
    """Resolve the effective registry root (env override, else default)."""
    return registry_root_from_env() or DEFAULT_REGISTRY_ROOT


def file_sha256(path) -> str:
    """Stream sha256 of a file (same format as the fingerprint audit)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class RegistryError(RuntimeError):
    """Base error for registry operations."""


class FingerprintMismatchError(RegistryError):
    """The declared fingerprint does not match the artifact's content."""


class RegistryIntegrityError(RegistryError):
    """A registered model file is missing or its hash no longer matches."""


@dataclass
class ModelEntry:
    """One registered model artifact (metadata only - never the file itself)."""

    registry_id: str
    asset: str
    timeframe: str
    model_path: str
    file_sha256: str
    fingerprint: Optional[str] = None
    trained_at_utc_ms: Optional[int] = None
    registered_at_utc_ms: int = 0
    metrics: dict = field(default_factory=dict)
    source_run_id: Optional[str] = None

    @property
    def key(self) -> str:
        """Activation-pointer key: 'ASSET|TIMEFRAME'."""
        return f"{self.asset}|{self.timeframe}"

    def to_dict(self) -> dict:
        return {
            "registry_id": self.registry_id,
            "asset": self.asset,
            "timeframe": self.timeframe,
            "model_path": self.model_path,
            "file_sha256": self.file_sha256,
            "fingerprint": self.fingerprint,
            "trained_at_utc_ms": self.trained_at_utc_ms,
            "registered_at_utc_ms": self.registered_at_utc_ms,
            "metrics": self.metrics,
            "source_run_id": self.source_run_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEntry":
        return cls(
            registry_id=d["registry_id"],
            asset=d["asset"],
            timeframe=d["timeframe"],
            model_path=d["model_path"],
            file_sha256=d["file_sha256"],
            fingerprint=d.get("fingerprint"),
            trained_at_utc_ms=d.get("trained_at_utc_ms"),
            registered_at_utc_ms=int(d.get("registered_at_utc_ms") or 0),
            metrics=dict(d.get("metrics") or {}),
            source_run_id=d.get("source_run_id"),
        )


def _bundle_fingerprint(model_path) -> Optional[str]:
    """Recompute the deterministic content fingerprint of a saved bundle.

    Returns None when the file is not a loadable model bundle (the registry
    then records the file sha256 only - the fingerprint audit script handles
    unrecognized formats).
    """
    try:
        import joblib
        from model.trainer import compute_model_fingerprint

        bundle = joblib.load(model_path)
        if not isinstance(bundle, dict) or "model" not in bundle:
            return None
        cols = bundle.get("feature_cols")
        if cols is None:
            return None
        return compute_model_fingerprint(bundle["model"], cols)
    except Exception as exc:  # noqa: BLE001 - cataloging must be robust
        logger.debug("fingerprint recompute failed for %s: %r", model_path, exc)
        return None


def _bundle_metadata(model_path) -> dict:
    """Best-effort read of a bundle's metadata dict ({} on any failure)."""
    try:
        import joblib

        bundle = joblib.load(model_path)
        if isinstance(bundle, dict):
            return dict(bundle.get("metadata") or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("metadata read failed for %s: %r", model_path, exc)
    return {}


def _iso_to_utc_ms(iso: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


class ModelRegistry:
    """Append-only catalog + atomic active-pointer for trained models."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_registry_root()
        self.index_path = self.root / _INDEX_NAME
        self.active_path = self.root / _ACTIVE_NAME

    # ------------------------------------------------------------------ #
    # register
    # ------------------------------------------------------------------ #
    def register(
        self,
        model_path,
        asset: str,
        timeframe: str,
        fingerprint: Optional[str] = None,
        trained_at_utc_ms: Optional[int] = None,
        metrics: Optional[dict] = None,
        source_run_id: Optional[str] = None,
        registered_at_utc_ms: Optional[int] = None,
    ) -> str:
        """Catalog a trained model file and return its registry_id.

        The physical file is NOT moved or modified. When ``fingerprint`` is
        given it is verified against the artifact's recomputed content
        fingerprint (and its own ``metadata.model_hash`` via the caller-supplied
        ``register_trained_model`` helper); a mismatch raises
        ``FingerprintMismatchError`` so a wrong/foreign artifact can never be
        registered under a false identity.
        """
        path = Path(model_path)
        if not path.exists():
            raise RegistryError(f"model file does not exist: {path}")
        asset = str(asset).upper()
        timeframe = str(timeframe).upper()

        sha = file_sha256(path)
        recomputed = _bundle_fingerprint(path)
        if fingerprint is not None and recomputed is not None and recomputed != fingerprint:
            raise FingerprintMismatchError(
                f"fingerprint mismatch for {path}: declared {fingerprint}, "
                f"recomputed {recomputed}"
            )
        if fingerprint is None:
            fingerprint = recomputed

        now_ms = int(time.time() * 1000)
        if trained_at_utc_ms is None:
            trained_at_utc_ms = now_ms
        reg_at = int(registered_at_utc_ms or trained_at_utc_ms)
        registry_id = f"{asset}-{timeframe}-{reg_at}-{sha[:8]}"

        existing = self.get(registry_id)
        if existing is not None:
            # Idempotent re-registration of the identical artifact.
            return registry_id

        entry = ModelEntry(
            registry_id=registry_id,
            asset=asset,
            timeframe=timeframe,
            model_path=str(path),
            file_sha256=sha,
            fingerprint=fingerprint,
            trained_at_utc_ms=int(trained_at_utc_ms),
            registered_at_utc_ms=reg_at,
            metrics=dict(metrics or {}),
            source_run_id=source_run_id,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        logger.info("registered %s (%s %s) -> %s", registry_id, asset, timeframe, path)
        return registry_id

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def _read_index(self) -> list[ModelEntry]:
        if not self.index_path.exists():
            return []
        entries: list[ModelEntry] = []
        with open(self.index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(ModelEntry.from_dict(json.loads(line)))
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("skipping malformed registry line: %r", exc)
        return entries

    def get(self, registry_id: str) -> Optional[ModelEntry]:
        for e in self._read_index():
            if e.registry_id == registry_id:
                return e
        return None

    def list_entries(self, asset: Optional[str] = None) -> list[ModelEntry]:
        entries = self._read_index()
        if asset is not None:
            a = str(asset).upper()
            entries = [e for e in entries if e.asset == a]
        return entries

    def history(self, asset: str, timeframe: str) -> list[ModelEntry]:
        """Entries for an asset+timeframe, oldest registration first."""
        a, tf = str(asset).upper(), str(timeframe).upper()
        entries = [e for e in self._read_index() if e.asset == a and e.timeframe == tf]
        entries.sort(key=lambda e: (e.registered_at_utc_ms, e.registry_id))
        return entries

    # ------------------------------------------------------------------ #
    # integrity
    # ------------------------------------------------------------------ #
    def verify(self, registry_id: str) -> bool:
        """Recompute the sha256 of the registered file and compare.

        False when the entry is unknown, the file is missing or the content
        hash drifted (corruption / silent overwrite).
        """
        entry = self.get(registry_id)
        if entry is None:
            return False
        path = Path(entry.model_path)
        if not path.exists():
            return False
        try:
            return file_sha256(path) == entry.file_sha256
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    # activation (atomic pointer swap)
    # ------------------------------------------------------------------ #
    def _read_active(self) -> dict:
        if not self.active_path.exists():
            return {}
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _write_active(self, data: dict) -> None:
        """Atomic pointer write: tmp file in the same dir + os.replace."""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.active_path.with_name(self.active_path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.active_path)

    def activate(self, registry_id: str) -> ModelEntry:
        """Point 'ASSET|TIMEFRAME' at registry_id, atomically.

        Refuses (RegistryIntegrityError) when the registered model file is
        missing or corrupt - a damaged artifact must never become active, and
        the existing active pointer stays untouched on refusal.
        """
        entry = self.get(registry_id)
        if entry is None:
            raise RegistryError(f"unknown registry_id: {registry_id}")
        if not self.verify(registry_id):
            raise RegistryIntegrityError(
                f"model file for {registry_id} is missing or corrupt "
                f"(sha256 mismatch); activation refused"
            )
        data = self._read_active()
        data[entry.key] = entry.registry_id
        self._write_active(data)
        logger.info("activated %s for %s", registry_id, entry.key)
        return entry

    def get_active(self, asset: str, timeframe: str) -> Optional[ModelEntry]:
        key = f"{str(asset).upper()}|{str(timeframe).upper()}"
        rid = self._read_active().get(key)
        if not rid:
            return None
        return self.get(rid)

    def rollback(
        self, asset: str, timeframe: str, to_registry_id: Optional[str] = None
    ) -> ModelEntry:
        """Activate the previous entry for an asset+timeframe.

        Without ``to_registry_id`` the entry registered immediately before the
        currently active one is activated. Raises RegistryError when there is
        no active model or no previous version to roll back to.
        """
        hist = self.history(asset, timeframe)
        if to_registry_id is not None:
            entry = self.get(to_registry_id)
            if entry is None:
                raise RegistryError(f"unknown registry_id: {to_registry_id}")
            if entry.asset != str(asset).upper() or entry.timeframe != str(timeframe).upper():
                raise RegistryError(
                    f"{to_registry_id} does not belong to {asset}/{timeframe}"
                )
            return self.activate(to_registry_id)

        active = self.get_active(asset, timeframe)
        if active is None:
            raise RegistryError(f"no active model for {asset}/{timeframe} to roll back from")
        ids = [e.registry_id for e in hist]
        try:
            idx = ids.index(active.registry_id)
        except ValueError:
            idx = len(ids)  # active entry not in history (foreign pointer)
        if idx <= 0:
            raise RegistryError(
                f"no previous entry to roll back to for {asset}/{timeframe}"
            )
        return self.activate(hist[idx - 1].registry_id)


def register_trained_model(
    model_path,
    asset: str,
    timeframe: str,
    source_run_id: Optional[str] = None,
    root=None,
) -> str:
    """Convenience wrapper used by the training pipeline.

    Reads the bundle's own metadata (``trained_at_utc``, ``model_hash``,
    config identity) and registers the artifact. Passing the stored
    ``metadata.model_hash`` as the declared fingerprint means registration
    also cross-checks the artifact's self-hash against the recomputed content
    fingerprint - a corrupted or hand-edited bundle is refused.
    """
    meta = _bundle_metadata(model_path)
    metrics = {
        k: meta[k]
        for k in (
            "bundle_schema_version",
            "effective_config_sha256",
            "class_counts",
            "sample_weight_mode",
        )
        if k in meta
    }
    return ModelRegistry(root).register(
        model_path,
        asset,
        timeframe,
        fingerprint=meta.get("model_hash"),
        trained_at_utc_ms=_iso_to_utc_ms(meta.get("trained_at_utc")) if meta.get("trained_at_utc") else None,
        metrics=metrics,
        source_run_id=source_run_id,
    )
