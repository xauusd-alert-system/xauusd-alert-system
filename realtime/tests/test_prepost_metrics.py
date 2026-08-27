"""Tests for realtime/prepost_metrics.py (dashboard pre/post comparison)."""
import os

import pandas as pd
import pytest

from realtime import prepost_metrics as pm


def _write_pair(tmp_path, asset_lower: str, tf: str, pre_rows, post_rows) -> None:
    def dump(name, rows):
        pd.DataFrame(rows).to_csv(os.path.join(tmp_path, name), index=False)

    suffix = f"_{tf}" if tf else ""
    dump(f"dir_prepost_{asset_lower}{suffix}_prefix.csv", pre_rows)
    dump(f"dir_prepost_{asset_lower}{suffix}_postfix.csv", post_rows)


def _rows(dirs, rs, pnls, variant="current"):
    return [
        {"fold_id": 0, "variant": variant, "entry_ts": 1700000000 + i,
         "direction": d, "session": "london", "regime": "range",
         "p_long": 0.6, "p_short": 0.4, "p_max": 0.6,
         "pnl": pnls[i], "R": rs[i], "exit_reason": "tp3_runner"}
        for i, (d, r, p) in enumerate(zip(dirs, rs, pnls))
    ]


def test_collect_prepost_basic(tmp_path):
    # two assets: XAUUSD with a pair, EURUSD with a pair
    _write_pair(tmp_path, "xauusd", "", _rows(["long"] * 3, [1.0, 1.0, -1.0], [10, 10, -10]),
                _rows(["long"] * 4, [1.0, 1.0, 1.0, -1.0], [10, 10, 10, -10]))
    _write_pair(tmp_path, "eurusd", "", _rows(["short"] * 2, [-1.0, -1.0], [-5, -5]),
                _rows(["short"] * 2, [0.5, -1.0], [5, -5]))
    out = pm.collect_prepost(str(tmp_path))
    assert out["available"] is True
    assert set(out["assets"]) == {"XAUUSD", "EURUSD"}
    xau = out["assets"]["XAUUSD"]
    assert xau["pre"]["n"] == 3 and xau["post"]["n"] == 4
    assert xau["pre"]["sum_r"] == 1.0 and xau["post"]["sum_r"] == 2.0
    assert xau["delta"]["sum_r"] == 1.0
    assert xau["pre"]["long_n"] == 3 and xau["post"]["long_n"] == 4
    eur = out["assets"]["EURUSD"]
    assert eur["pre"]["pf"] == 0.0 and eur["post"]["pf"] == 0.5


def test_collect_prepost_prefers_newest_tf(tmp_path):
    # BTCUSD has both default and m5 pairs; the newer file must win.
    _write_pair(tmp_path, "btcusd", "", _rows(["short"] * 2, [-1.0, -1.0], [-1, -1]),
                _rows(["short"] * 2, [-1.0, -1.0], [-1, -1]))
    _write_pair(tmp_path, "btcusd", "m5", _rows(["short"] * 5, [1.0] * 5, [1] * 5),
                _rows(["short"] * 5, [1.0] * 5, [1] * 5))
    out = pm.collect_prepost(str(tmp_path))
    btc = out["assets"]["BTCUSD"]
    assert btc["tf"] == "m5"
    assert btc["pre"]["n"] == 5  # m5 pair won
    superseded = [e for e in out["extra_pairs"] if e.get("superseded_by") == "m5"]
    assert superseded and superseded[0]["asset"] == "BTCUSD"


def test_collect_prepost_missing_pair(tmp_path):
    # Only a prefix file, no postfix -> reported as extra, not an asset.
    pd.DataFrame(_rows(["long"], [1.0], [1.0])).to_csv(
        os.path.join(tmp_path, "dir_prepost_gbpusd_prefix.csv"), index=False)
    out = pm.collect_prepost(str(tmp_path))
    assert out["available"] is False
    assert "GBPUSD" not in out["assets"]
    assert any(e["asset"] == "GBPUSD" for e in out["extra_pairs"])


def test_collect_prepost_empty_dir(tmp_path):
    out = pm.collect_prepost(str(tmp_path))
    assert out["available"] is False
    assert out["assets"] == {}
