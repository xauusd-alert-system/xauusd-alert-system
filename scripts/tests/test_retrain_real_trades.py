"""
Step 5d regression tests for #26 (real trades merged into retraining) and #27
(nightly retrain must not silently succeed - a missing real-trade payload must
surface as a non-zero exit code that scripts/overnight.py stage 4 turns into a
FAILED/notified stage).

Tests are pure unit tests: retrain_with_real_trades imports a per-asset
`retrain_asset()` and a `main()` that iterates it, both of which are tested here
without spawning real subprocesses or touching a real SQLite DB.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.retrain_with_real_trades import (
    EXIT_OK,
    EXIT_PAYLOAD_MISSING,
    load_config,
    main,
    prepare_real_trades_df,
    retrain_asset,
)

FEATURE_COLS = ["rsi", "atr", "ema_9"]


def _trade_df(rows: list) -> pd.DataFrame:
    """Build a minimal executed_trades-style DataFrame (schema from data/trade_logger.py)."""
    import json

    data = []
    for i, row in enumerate(rows):
        rec = {
            "ticket": row.get("ticket", 1000 + i),
            "symbol": row.get("symbol", "XAUUSD"),
            "bias": row["bias"],
            "entry_time": row.get("entry_time", 1700000000 + i),
            "entry_price": row.get("entry_price", 2000.0),
            "close_time": row.get("close_time"),
            "close_price": row.get("close_price"),
            "pnl": row.get("pnl"),
            "outcome": row["outcome"],
            "features": row.get("features") or {},
        }
        if isinstance(rec["features"], dict):
            rec["features"] = json.dumps(rec["features"])
        data.append(rec)
    return pd.DataFrame(data)


# --------------------------------------------------------------------------- #
# #26 - real executed trades are merged into retraining with the documented
#       label mapping: long-win->1, long-loss->0, short-win->0, short-loss->1.
# --------------------------------------------------------------------------- #
def test_prepare_real_trades_maps_outcomes_and_fills_missing_features():
    trades = _trade_df(
        [
            {"bias": "long", "outcome": 1, "features": {"rsi": 70.0}},   # -> 1 (long win), atr/ema_9 -> 0.0
            {"bias": "long", "outcome": 0, "features": {"rsi": 30.0}},   # -> 0 (long loss)
            {"bias": "short", "outcome": 1, "features": {"rsi": 25.0}},  # -> 0 (short win)
            {"bias": "short", "outcome": 0, "features": {"rsi": 75.0}},  # -> 1 (short loss)
        ]
    )
    X, y = prepare_real_trades_df(trades, FEATURE_COLS)

    assert list(X.columns) == FEATURE_COLS
    assert list(y) == [1, 0, 0, 1]
    # Missing features default to 0.0 (never a partial/ragged row).
    assert (X["atr"] == 0.0).all()
    assert (X["ema_9"] == 0.0).all()


def test_prepare_real_trades_drops_malformed_rows_without_crashing():
    trades = _trade_df(
        [
            {"bias": "long", "outcome": 1, "features": {"rsi": 5.0}},   # kept
            {"bias": "long", "outcome": 1, "features": None},            # empty -> dropped
            {"bias": "weird", "outcome": 1, "features": {"rsi": 5.0}},   # bad bias -> dropped
            {"bias": "short", "outcome": "x", "features": {"rsi": 5.0}},  # non-numeric outcome -> dropped
            {"bias": "short", "outcome": None, "features": {"rsi": 5.0}}, # missing outcome -> dropped
        ]
    )
    X, y = prepare_real_trades_df(trades, FEATURE_COLS)

    assert list(y) == [1]
    assert list(X.columns) == FEATURE_COLS


def test_prepare_real_trades_empty_input_returns_empty_contract():
    empty = pd.DataFrame(columns=["ticket", "bias", "outcome", "features"])
    X, y = prepare_real_trades_df(empty, FEATURE_COLS)

    assert X.empty
    assert list(X.columns) == FEATURE_COLS
    assert len(y) == 0


def test_retrain_asset_skips_merge_and_returns_not_ok_in_three_class_mode(tmp_path, monkeypatch):
    """#27: include_zero_class=true skips the real-trade merge, but the return
    must report ok=False so main() returns non-zero instead of a silent green."""
    import scripts.retrain_with_real_trades as mod

    cfg = load_config()
    cfg["model"]["include_zero_class"] = True

    # No candles -> early "no_candles" return path; we only need the flags/exit
    # semantics, not a full SQLite/data + train pipeline.
    monkeypatch.setattr(mod, "read_candles", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(mod, "read_executed_trades", lambda *a, **k: _trade_df(
        [{"bias": "long", "outcome": 1, "features": {"rsi": 5.0}}]
    ))

    stats = retrain_asset("XAUUSD", cfg)

    assert stats["ok"] is False
    assert stats["real_trades"] == 0
    assert stats["reason"] == "no_candles"


def test_retrain_asset_skips_merge_and_returns_not_ok_in_regime_feature_mode(tmp_path, monkeypatch):
    """#27: use_regime_feature=true also skips the real-trade merge and must
    report ok=False."""
    import scripts.retrain_with_real_trades as mod

    cfg = load_config()
    cfg["model"]["use_regime_feature"] = True

    monkeypatch.setattr(mod, "read_candles", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(mod, "read_executed_trades", lambda *a, **k: _trade_df(
        [{"bias": "long", "outcome": 1, "features": {"rsi": 5.0}}]
    ))

    stats = retrain_asset("XAUUSD", cfg)

    assert stats["ok"] is False
    assert stats["real_trades"] == 0
    assert stats["reason"] == "no_candles"


def test_real_trade_merge_is_default_off_until_debiasing_contract_exists():
    cfg = load_config()
    assert cfg["retraining"]["real_trade_merge_enabled"] is False


# --------------------------------------------------------------------------- #
# #27 - main() must surface problems as a non-zero exit code.
# --------------------------------------------------------------------------- #
def test_main_returns_ok_when_all_assets_retrain_successfully(tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.setattr("scripts.retrain_with_real_trades.load_config", lambda *a, **k: cfg)

    def fake_retrain(asset_key, cfg_inner):
        return {"asset": asset_key, "ok": True, "samples": 1000,
                "real_trades": 10, "reason": "ok"}

    monkeypatch.setattr("scripts.retrain_with_real_trades.retrain_asset", fake_retrain)

    assert main() == EXIT_OK


def test_main_returns_nonzero_when_merge_skipped_for_all_assets(tmp_path, monkeypatch):
    """#27: if every asset skips the real-trade merge (3-class/regime mode),
    main() must return EXIT_PAYLOAD_MISSING (1), not silently exit 0."""
    cfg = load_config()
    monkeypatch.setattr("scripts.retrain_with_real_trades.load_config", lambda *a, **k: cfg)

    def fake_retrain(asset_key, cfg_inner):
        return {"asset": asset_key, "ok": False, "samples": 1000,
                "real_trades": 0, "reason": "skip_merge_three_class"}

    monkeypatch.setattr("scripts.retrain_with_real_trades.retrain_asset", fake_retrain)

    assert main() == EXIT_PAYLOAD_MISSING


def test_main_returns_nonzero_when_any_asset_hard_fails(tmp_path, monkeypatch):
    """#27: a hard failure (exception, no candles, too few samples) must also
    surface as EXIT_PAYLOAD_MISSING rather than a silent exit 0."""
    cfg = load_config()
    monkeypatch.setattr("scripts.retrain_with_real_trades.load_config", lambda *a, **k: cfg)

    def fake_retrain(asset_key, cfg_inner):
        raise RuntimeError("boom")

    monkeypatch.setattr("scripts.retrain_with_real_trades.retrain_asset", fake_retrain)

    assert main() == EXIT_PAYLOAD_MISSING
