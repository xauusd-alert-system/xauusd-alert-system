"""
Tests for scripts/diag_trade_quality.py using synthetic data.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from scripts.deflated_sharpe import (
    _SYNTH_DEFAULTS,
    _inject_biased_probs,
    _make_synthetic_wf_df,
)
from scripts.diag_trade_quality import collect_trades_for_variant


def _synthetic_xauusd_with_probs():
    spec = _SYNTH_DEFAULTS["XAUUSD"]
    df = _make_synthetic_wf_df(6000, spec["price"], spec["atr"], spec["freq"])
    return _inject_biased_probs(df)


def test_collect_trades_for_variant_synthetic():
    """Ensure the collector returns records with expected fields on synthetic data."""
    cfg = load_config()
    df = _synthetic_xauusd_with_probs()
    records = collect_trades_for_variant(cfg, "XAUUSD", df, "current", {}, max_folds=2)
    assert isinstance(records, list)
    if records:
        # Must have all diagnostic fields
        required = {
            "variant",
            "entry_ts",
            "session",
            "regime",
            "p_long",
            "p_short",
            "p_max",
            "pnl",
            "R",
            "exit_reason",
            "atr",
        }
        for r in records[:1]:
            assert required.issubset(r.keys())
        # R calculations should be finite
        assert all(np.isfinite(r["R"]) for r in records)


def test_collect_trades_consistent_with_variant_name():
    """The variant field in records must match the requested variant."""
    cfg = load_config()
    df = _synthetic_xauusd_with_probs()
    records = collect_trades_for_variant(cfg, "XAUUSD", df, "wide", {"signal_grid": {"stop_mult": 4.0}}, max_folds=1)
    if records:
        assert all(r["variant"] == "wide" for r in records)
