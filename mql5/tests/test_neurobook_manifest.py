"""Contract tests for the vendored NeuroBook pin (TZ_BOOKS task T-01).

The MQL5 sources themselves cannot run in this Linux sandbox, so these tests
lock the *version pin*: the manifest must be structurally valid, must pin the
documented upstream identifiers, and the fetch script must expose the three
documented modes. If the vendor tree has been fetched, it is additionally
verified against the inventory (skipped otherwise).
"""
from __future__ import annotations

import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
NEUROBOOK_DIR = os.path.join(os.path.dirname(HERE), "NeuroBook")
MANIFEST_PATH = os.path.join(NEUROBOOK_DIR, "NEUROBOOK_MANIFEST.json")
FETCH_SCRIPT = os.path.join(NEUROBOOK_DIR, "fetch_neurobook.py")
VENDOR_DIR = os.path.join(NEUROBOOK_DIR, "vendor")


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_manifest_pins_all_upstream_identifiers(manifest: dict):
    pin = manifest["version_pin"]
    assert pin["codebase_url"] == "https://www.mql5.com/en/code/48097"
    assert pin["codebase_zip_url"].endswith("/48097.zip")
    assert pin["forge_url"] == "https://forge.mql5.io/rosh/NeuroBook"
    assert pin["forge_branch"] == "main"
    # full 40-hex git commit => binary-identical pin, not a moving branch tip
    assert len(pin["forge_commit"]) == 40
    int(pin["forge_commit"], 16)
    assert "Shared Projects" in pin["shared_project_path"]


def test_manifest_inventory_is_complete_and_unique(manifest: dict):
    files = manifest["files"]
    assert manifest["file_count"] == len(files) == 53
    paths = [f["path"] for f in files]
    assert len(set(paths)) == len(paths), "duplicate entries in inventory"
    # every entry must live under the documented MQL5 tree layout
    prefixes = ("Include/NeuroNetworksBook/", "Experts/NeuroNetworksBook/",
                "Scripts/NeuroNetworksBook/")
    assert all(p.startswith(prefixes) for p in paths)
    # sanity of sizes
    for f in files:
        assert 0.5 < f["size_kb"] < 200, f"implausible size for {f['path']}"


def test_manifest_contains_book_reference_modules(manifest: dict):
    """The inventory must include the modules the other TZ tasks rely on."""
    paths = {f["path"] for f in manifest["files"]}
    for required in (
        # declarative layer description API (T-20)
        "Include/NeuroNetworksBook/realization/layerdescription.mqh",
        # the NN library core (T-04/T-09 reference)
        "Include/NeuroNetworksBook/realization/neuronnet.mqh",
        "Include/NeuroNetworksBook/realization/neuronbase.mqh",
        # attention architectures (T-09)
        "Include/NeuroNetworksBook/realization/neuronattention.mqh",
        "Include/NeuroNetworksBook/realization/neuronmhattention.mqh",
        # LSTM (T-09)
        "Include/NeuroNetworksBook/realization/neuronlstm.mqh",
        # OpenCL kernels (T-18)
        "Include/NeuroNetworksBook/realization/opencl_program.cl",
        # sample generator pattern (T-02)
        "Scripts/NeuroNetworksBook/initial_data/create_initial_data.mq5",
        # gradient check scripts (T-11)
        "Scripts/NeuroNetworksBook/perceptron/check_gradient_percp.mq5",
        "Scripts/NeuroNetworksBook/rnn/check_gradient_lstm.mq5",
        # EA template (T-04/T-10 pattern source)
        "Experts/NeuroNetworksBook/ea_template.mq5",
    ):
        assert required in paths, f"missing reference module {required}"


def test_fetch_script_exists_with_documented_modes():
    with open(FETCH_SCRIPT, "r", encoding="utf-8") as fh:
        src = fh.read()
    for mode in ("--verify", "--from-forge", "codebase_zip_url", "forge_commit"):
        assert mode in src
    # the script must consult the committed manifest, not a baked-in URL only
    assert "NEUROBOOK_MANIFEST.json" in src


def test_vendor_tree_is_git_ignored():
    repo_root = os.path.abspath(os.path.join(HERE, "..", ".."))
    gitignore = os.path.join(repo_root, ".gitignore")
    with open(gitignore, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "mql5/NeuroBook/vendor/" in content


@pytest.mark.skipif(not os.path.isdir(VENDOR_DIR), reason="vendor tree not fetched")
def test_fetched_vendor_tree_matches_manifest(manifest: dict):
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_neurobook", FETCH_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ok, problems = mod.verify(manifest)
    assert ok, f"vendor drift: {problems}"
