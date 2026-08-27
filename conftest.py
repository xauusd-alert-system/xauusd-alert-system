"""
Root-level pytest fixtures.  Wraps tests.builder so any test suite can
use ``cfg``, ``pipeline``, ``signal``, ``position``, ``stub_predictor``,
``stub_connector`` fixtures without importing the builder directly.

All fixtures are function-scoped (fresh per test) and accept optional
overrides via indirect parametrize.
"""
import sys
import os

# logs/ws_live_test.py is a manual live-trading smoke script (needs MetaTrader5
# + websockets + an open MT5 session), NOT a test. Exclude it from collection so
# `pytest -q` at the repo root does not error on the optional websockets import.
collect_ignore_glob = ["logs/ws_live_test.py"]

import pytest

# Ensure project root is on sys.path for all test suites
_root = os.path.abspath(os.path.dirname(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.builder import (
    build_cfg,
    build_pipeline,
    build_signal,
    build_position,
    build_risk,
    StubPredictor,
    StubMT5,
    StubConnector,
)


# ---------------------------------------------------------------------------
# cfg
# ---------------------------------------------------------------------------
@pytest.fixture
def cfg():
    """Fresh test cfg dict with sensible defaults for all 5 assets."""
    return build_cfg()


@pytest.fixture
def cfg_no_manifest():
    """cfg with provenance manifest gate disabled."""
    return build_cfg(validation_require_provenance_manifest=False)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
@pytest.fixture
def pipeline(cfg):
    """RealtimePipeline in mock mode, no model loaded."""
    return build_pipeline(cfg=cfg, data_mode="mock", model_path=None)


@pytest.fixture
def pipeline_with_model(cfg):
    """RealtimePipeline in mock mode with the production model (if it exists)."""
    model_path = cfg["assets"]["XAUUSD"]["model_path"]
    if not os.path.isfile(model_path):
        pytest.skip("production model not on disk")
    return build_pipeline(cfg=cfg, asset="XAUUSD", data_mode="mock",
                          model_path=model_path)


# ---------------------------------------------------------------------------
# Signal / position / risk
# ---------------------------------------------------------------------------
@pytest.fixture
def signal():
    """Neutral no_trade signal dict."""
    return build_signal()


@pytest.fixture
def position():
    """Sample long position dict."""
    return build_position()


@pytest.fixture
def risk():
    """Default risk config."""
    return build_risk()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_predictor():
    return StubPredictor()


@pytest.fixture
def stub_mt5():
    return StubMT5()


@pytest.fixture
def stub_connector():
    return StubConnector()
