"""Tests for the T-16 end-to-end producer and the external-candles importer.

Chain under test (all on deterministic synthetic history, no terminal):

    MT4-style CSV -> import_external_candles -> sqlite candles
    -> publish_book_signals (features -> normalization -> ensemble -> vote)
    -> ml_signals bridge row (status 'new', features_hash, idempotent)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from model.book_nn import (  # noqa: E402
    BookNetwork,
    book_fc_baseline_description,
    book_lstm_description,
)
from model.sample_generator import synthetic_ohlcv  # noqa: E402
from scripts.import_external_candles import import_csv  # noqa: E402
from scripts.publish_book_signals import (  # noqa: E402
    compute_features_hash,
    publish,
)

WINDOW = 16
HORIZON = 12


def _write_mt4_csv(path: Path, df: pd.DataFrame) -> None:
    lines = ["Date;Open;High;Low;Close;Volume"]
    for t, r in zip(df.index, df.itertuples()):
        lines.append(f"{pd.Timestamp(t):%Y.%m.%d %H:%M};{r.open};{r.high};"
                     f"{r.low};{r.close};{int(r.volume)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prepare(tmp_path: Path, n: int = 800, seed: int = 7):
    """Imported candles db + a models dir with tiny (untrained) book nets."""
    df = synthetic_ohlcv(n=n, seed=seed)
    csv = tmp_path / "hist.csv"
    _write_mt4_csv(csv, df)
    db = str(tmp_path / "ext.sqlite")
    meta = import_csv(str(csv), db, "XAUUSD", "M15")
    assert meta["rows_imported"] == n and meta["rows_rejected"] == 0

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    # untrained nets are enough: the producer must faithfully relay whatever
    # the ensemble says, and tiny nets keep the test fast
    fc = BookNetwork(book_fc_baseline_description(hidden=8, output_dim=2),
                     WINDOW, 7, seed=1)
    fc.save(str(models_dir / "book_fc"))
    lstm = BookNetwork(book_lstm_description(hidden=8, output_dim=2),
                       WINDOW, 7, seed=2)
    lstm.save(str(models_dir / "book_lstm"))

    from model.sample_generator import generate_book_samples
    cfg = {"window": WINDOW, "horizon": HORIZON, "extended": False,
           "target_mode": "multi_horizon"}
    generate_book_samples(df, cfg,
                          norm_params_path=str(models_dir
                                               / "normalization_params.json"))
    return str(models_dir), db, df


def _bridge_rows(bridge: str) -> list[tuple]:
    if not Path(bridge).exists():   # flat signals never create the bridge
        return []
    con = sqlite3.connect(bridge)
    try:
        return con.execute(
            "SELECT intent_id, asset, direction, probability, entry_price, "
            "sl_price, tp_price, horizon_bars, status, features_hash "
            "FROM ml_signals").fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- importer

def test_importer_rejects_ohlcv_violations(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "Date;Open;High;Low;Close;Volume\n"
        "2024-01-01 00:00;10;12;9;11;100\n"
        "2024-01-01 00:05;11;10;9;10;100\n"   # high < open/close -> invalid
        "2024-01-01 00:10;10;11;9;10;100\n",
        encoding="utf-8")
    meta = import_csv(str(csv), str(tmp_path / "bad.sqlite"), "XAUUSD", "M5")
    assert meta["rows_csv"] == 3
    assert meta["rows_rejected"] == 1
    assert meta["rows_imported"] == 2

    con = sqlite3.connect(str(tmp_path / "bad.sqlite"))
    try:
        times = [r[0] for r in con.execute(
            "SELECT time FROM candles ORDER BY time").fetchall()]
    finally:
        con.close()
    assert len(times) == 2


def test_importer_is_idempotent(tmp_path):
    df = synthetic_ohlcv(n=50, seed=3)
    csv = tmp_path / "h.csv"
    _write_mt4_csv(csv, df)
    db = str(tmp_path / "idem.sqlite")
    import_csv(str(csv), db, "XAUUSD", "M5")
    import_csv(str(csv), db, "XAUUSD", "M5")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 50
    finally:
        con.close()


# ----------------------------------------------------------------- hashing

def test_features_hash_is_deterministic_and_sensitive():
    a = np.arange(2 * 3, dtype=float).reshape(2, 3)
    cols = ["x", "y", "z"]
    h1 = compute_features_hash(a, cols)
    assert h1 == compute_features_hash(a.copy(), list(cols))
    assert compute_features_hash(a + 0.5, cols) != h1
    assert compute_features_hash(a, list(reversed(cols))) != h1
    assert h1.startswith("sha256:") and len(h1) == 7 + 64


# --------------------------------------------------------------- producer

def test_publish_end_to_end_writes_idempotent_intent(tmp_path):
    models_dir, db, df = _prepare(tmp_path)
    bridge = str(tmp_path / "bridge.sqlite")

    # thresholds that guarantee a non-flat vote from the deterministic
    # ensemble: trade_level 0.5 -> every mean probability is long or short
    res = publish(models_dir, db, bridge, "XAUUSD", "M15",
                  window=WINDOW, horizon=HORIZON,
                  trade_level=0.5, min_agreement=0.0, warmup_bars=200)
    assert res["signal"] in ("long", "short")
    assert res["written"] is True
    assert res["models"] == ["fc", "lstm"]
    assert res["features_hash"].startswith("sha256:")

    rows = _bridge_rows(bridge)
    assert len(rows) == 1
    (intent_id, asset, direction, probability, entry, sl, tp, horizon,
     status, fhash) = rows[0]
    assert intent_id == res["intent_id"] and asset == "XAUUSD"
    assert status == "new"
    assert direction in (1, -1)
    assert horizon == HORIZON
    assert fhash == res["features_hash"]
    # probability is the probability OF the signalled direction
    p_up = res["mean_probability_p_up"]
    assert probability == pytest.approx(p_up if direction == 1 else 1 - p_up,
                                        abs=1e-6)
    # geometry: SL on the losing side, TP on the winning side, TP further
    if direction == 1:
        assert sl < entry < tp
    else:
        assert tp < entry < sl
    assert entry == pytest.approx(float(df["close"].iloc[-1]))

    # re-running on the same bar must not duplicate the intent
    res2 = publish(models_dir, db, bridge, "XAUUSD", "M15",
                   window=WINDOW, horizon=HORIZON,
                   trade_level=0.5, min_agreement=0.0, warmup_bars=200)
    assert res2["intent_id"] == res["intent_id"]
    assert len(_bridge_rows(bridge)) == 1


def test_publish_flat_sends_nothing(tmp_path):
    models_dir, db, _ = _prepare(tmp_path, seed=11)
    bridge = str(tmp_path / "bridge2.sqlite")
    res = publish(models_dir, db, bridge, "XAUUSD", "M15",
                  window=WINDOW, horizon=HORIZON,
                  trade_level=0.6, min_agreement=0.6, warmup_bars=200)
    if res["signal"] == "flat":
        assert res["written"] is False
        assert _bridge_rows(bridge) == []
    else:  # deterministic ensemble happened to clear the bar -> still valid
        assert res["written"] is True
        assert len(_bridge_rows(bridge)) == 1


def test_publish_refuses_mismatched_normalization(tmp_path):
    models_dir, db, _ = _prepare(tmp_path)
    # break the params: drop one column -> producer must fail closed
    path = Path(models_dir) / "normalization_params.json"
    params = json.loads(path.read_text(encoding="utf-8"))
    col = params["columns"][0]
    params["columns"] = params["columns"][1:]
    params["center"].pop(col, None)
    params["scale"].pop(col, None)
    path.write_text(json.dumps(params), encoding="utf-8")
    with pytest.raises((KeyError, ValueError)):
        publish(models_dir, db, str(tmp_path / "b.sqlite"), "XAUUSD", "M15",
                window=WINDOW, horizon=HORIZON, warmup_bars=200)


def test_publish_requires_enough_history(tmp_path):
    models_dir, _, df = _prepare(tmp_path)
    small_csv = tmp_path / "small.csv"
    _write_mt4_csv(small_csv, df.head(50))
    small_db = str(tmp_path / "small.sqlite")
    import_csv(str(small_csv), small_db, "XAUUSD", "M15")
    with pytest.raises(ValueError):
        publish(models_dir, small_db, str(tmp_path / "b.sqlite"),
                "XAUUSD", "M15", window=WINDOW, horizon=HORIZON,
                warmup_bars=200)
