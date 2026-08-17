from __future__ import annotations
import hashlib
import json
from pathlib import Path
import yaml


def load_strategy_spec(path: str | None = None) -> dict:
    source = Path(path) if path else Path(__file__).with_name("strategy_spec.yaml")
    with source.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def canonical_hash(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def strategy_identity(cfg: dict, model_metadata: dict | None = None) -> dict:
    spec = load_strategy_spec()
    return {
        "strategy_version": spec["strategy_version"],
        "strategy_spec_hash": canonical_hash(spec),
        "config_hash": canonical_hash(cfg),
        "model_hash": (model_metadata or {}).get("effective_config_sha256"),
    }
