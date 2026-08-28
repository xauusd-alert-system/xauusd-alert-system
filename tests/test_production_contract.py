import sqlite3

import numpy as np
import pandas as pd
import pytest

from config.loader import effective_asset_config
from data.execution_ledger import (
    broker_spread_report,
    execution_cost_report,
    log_execution_attempt,
)
from data.storage import init_schema, read_candles, upsert_candles
from model.trainer import load_model, save_model
from model.uniqueness import aligned_uniqueness_weights
from scripts import train_mt5


def test_effective_asset_config_merges_target_and_model_without_mutating_source():
    cfg = {
        "labeling": {"event": "barrier", "horizon_candles_n": 36},
        "model": {"include_zero_class": False},
        "assets": {"XAUUSD": {
            "labeling": {"event": "traded"},
            "model": {"sample_weight_mode": "uniqueness"},
        }},
    }
    effective = effective_asset_config(cfg, "XAUUSD")
    assert effective["labeling"] == {"event": "traded", "horizon_candles_n": 36}
    assert effective["model"]["include_zero_class"] is False
    assert effective["model"]["sample_weight_mode"] == "uniqueness"
    assert cfg["labeling"]["event"] == "barrier"
    with pytest.raises(KeyError, match="Unknown asset_key"):
        effective_asset_config(cfg, "UNKNOWN")


def test_production_build_full_df_passes_asset_key_to_label_contract(monkeypatch):
    raw = pd.DataFrame({"close": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9]})
    cfg = {
        "features": {"structure_lookback": 2},
        "labeling": {"event": "barrier"},
        "model": {},
        "ensemble": {},
        "signal_grid": {},
        "assets": {"XAUUSD": {"labeling": {"event": "traded"}}},
    }
    for name in ("build_all_indicators", "add_order_flow_features", "candle_anatomy",
                 "add_regime_indicators"):
        monkeypatch.setattr(train_mt5, name, lambda df, *args, **kwargs: df)
    monkeypatch.setattr(train_mt5, "detect_structure", lambda df, **kwargs: df)
    monkeypatch.setattr(train_mt5, "classify_regime_series", lambda df, cfg: pd.Series(["range"] * len(df)))
    seen = {}

    def fake_labels(df, cfg, asset_key=None):
        seen["asset_key"] = asset_key
        seen["event"] = cfg["labeling"]["event"]
        return pd.Series([1.0, -1.0], index=df.index)

    monkeypatch.setattr(train_mt5, "generate_labels_from_config", fake_labels)
    out = train_mt5.build_full_df(raw, cfg, asset_key="XAUUSD")
    assert seen["asset_key"] == "XAUUSD"
    assert seen["event"] == "traded"
    assert out["label"].tolist() == [1.0, -1.0]


def test_train_mt5_cutoff_is_applied_to_raw_candles_before_features():
    raw = pd.DataFrame({
        "timestamp_utc": [
            int(pd.Timestamp("2026-08-07T23:45:00Z").timestamp()),
            int(pd.Timestamp("2026-08-08T00:00:00Z").timestamp()),
        ],
        "close": [1.0, 2.0],
    })
    got = train_mt5.truncate_raw_before(raw, "2026-08-08", "XAUUSD")
    assert got["close"].tolist() == [1.0]


def test_purged_oos_calibration_report_is_embeddable():
    class Dummy:
        classes_ = np.array([0, 1])
        def predict_proba(self, X):
            p = np.linspace(0.2, 0.8, len(X))
            return np.column_stack([1.0 - p, p])

    X = pd.DataFrame({"x": range(10)})
    y = pd.Series([0, 0, 0, 0, 1, 0, 1, 1, 1, 1])
    report = train_mt5._purged_oos_calibration(Dummy(), X, y, "XAUUSD")
    assert report["available"] is True
    assert report["scope"] == "purged_production_holdout"
    assert report["n_samples"] == 10
    assert "brier_score" in report and "ece" in report


def test_model_bundle_persists_optional_contract_metadata(tmp_path):
    path = str(tmp_path / "model.joblib")
    save_model({"fake": "model"}, ["rsi"], path, metadata={"label_event": "traded"})
    bundle = load_model(path)
    assert bundle["feature_cols"] == ["rsi"]
    assert bundle["metadata"]["label_event"] == "traded"


def test_uniqueness_weights_align_after_rows_are_dropped():
    source = pd.Index([10, 20, 30, 40, 50, 60, 70])
    selected = pd.Index([20, 40])
    got = aligned_uniqueness_weights(source, selected, horizon=2)
    assert len(got) == 2
    assert np.all(got > 0)
    with pytest.raises(ValueError, match="alignment failed"):
        aligned_uniqueness_weights(source, pd.Index([999]), horizon=2)


def _candles(with_optional=True):
    data = {
        "timestamp_utc": [1, 2], "open": [10, 11], "high": [12, 13],
        "low": [9, 10], "close": [11, 12], "volume": [100, 110],
        "session": ["london", "london"],
    }
    if with_optional:
        data.update({"spread": [25, 30], "real_volume": [5, 7]})
    return pd.DataFrame(data)


def test_storage_migrates_and_round_trips_broker_market_fields(tmp_path):
    path = str(tmp_path / "market.sqlite")
    # Simulate the previous symbol-aware schema with no optional broker fields.
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE ohlcv_m15 (
            symbol TEXT NOT NULL, timestamp_utc INTEGER NOT NULL, open REAL NOT NULL,
            high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume REAL NOT NULL, session TEXT NOT NULL,
            PRIMARY KEY(symbol, timestamp_utc))""")
    init_schema(path, ["M15"])
    upsert_candles(path, "M15", "XAUUSD", _candles())
    got = read_candles(path, "M15", "XAUUSD")
    assert got["spread"].tolist() == [25.0, 30.0]
    assert got["real_volume"].tolist() == [5.0, 7.0]
    spread_report = broker_spread_report(path, "M15", "XAUUSD")
    assert spread_report["unit"] == "broker_points"
    assert spread_report["observations"] == 2
    assert spread_report["spread"]["p50"] == 27.5

    # A later source lacking optional fields must not erase observed broker data.
    upsert_candles(path, "M15", "XAUUSD", _candles(with_optional=False))
    got2 = read_candles(path, "M15", "XAUUSD")
    assert got2["spread"].tolist() == [25.0, 30.0]


def test_execution_ledger_reports_fills_rejections_latency_and_slippage(tmp_path):
    path = str(tmp_path / "fills.sqlite")
    log_execution_attempt(
        path, asset_key="XAUUSD", broker_symbol="GOLD", action="open", side="buy",
        requested_at_ms=1000, completed_at_ms=1012, requested_price=2000.0,
        filled_price=2000.2, volume_requested=0.1, volume_filled=0.1,
        status="filled", retcode=10009,
    )
    log_execution_attempt(
        path, asset_key="XAUUSD", broker_symbol="GOLD", action="open", side="sell",
        requested_at_ms=2000, completed_at_ms=2020, requested_price=2001.0,
        status="rejected", retcode=10004, rejection_reason="requote",
    )
    report = execution_cost_report(path, "XAUUSD")
    assert report["attempts"] == 2
    assert report["fills"] == 1
    assert report["rejections"] == 1
    assert report["rejection_rate"] == 0.5
    assert report["adverse_slippage_price_units"]["p50"] == pytest.approx(0.2)
