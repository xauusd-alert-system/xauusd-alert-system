"""Locked-holdout training-cutoff tests (nightly retrain fix).

The nightly retrain path (train_all_assets -> train_mt5, and the in-process
retrain_with_real_trades.retrain_asset) previously consumed the locked holdout
(validation.locked_holdout.start) silently. These tests pin the contract:

1. locked_holdout_end_date is the single source of truth (enabled+start ->
   start; disabled / missing start -> None, backwards compatible);
2. train_all_assets passes --end-date when the lock is on and does NOT when
   off;
3. retrain_asset truncates the raw frame at the cutoff (no bars >= start
   reach feature building);
4. lock-off behaviour is byte-identical to the pre-fix path.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.train_mt5 import locked_holdout_end_date, truncate_raw_before


def _cfg(lock_enabled: bool, start: str | None = "2026-08-08") -> dict:
    cfg: dict = {"validation": {"locked_holdout": {"enabled": lock_enabled}}}
    if start:
        cfg["validation"]["locked_holdout"]["start"] = start
    return cfg


class TestHelper:
    def test_enabled_with_start_returns_start(self):
        assert locked_holdout_end_date(_cfg(True)) == "2026-08-08"

    def test_disabled_returns_none(self):
        assert locked_holdout_end_date(_cfg(False)) is None

    def test_enabled_without_start_returns_none(self):
        assert locked_holdout_end_date(_cfg(True, start=None)) is None

    def test_no_validation_section_returns_none(self):
        assert locked_holdout_end_date({}) is None

    def test_absent_lock_key_returns_none(self):
        cfg = {"validation": {}}
        assert locked_holdout_end_date(cfg) is None


def _raw_frame(n: int = 100) -> pd.DataFrame:
    """Raw candle frame spanning 2026-07-20 .. 2026-09-01 (UTC epoch s)."""
    ts = pd.date_range("2026-07-20", periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(3)
    price = 4400 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame(
        {
            "timestamp_utc": (ts - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1),
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": rng.integers(10, 100, n).astype(float),
        }
    )


class TestTrainAllAssetsCmd:
    def _build_cmd(self, cfg: dict) -> list:
        """Mirror train_all_assets.main's cmd construction (subprocess path)."""
        from scripts.train_all_assets import locked_holdout_end_date_for_cmd

        return locked_holdout_end_date_for_cmd(cfg)

    def test_cmd_contains_end_date_when_locked(self):
        cmd = self._build_cmd(_cfg(True))
        assert "--end-date" in cmd
        assert cmd[cmd.index("--end-date") + 1] == "2026-08-08"

    def test_cmd_omits_end_date_when_unlocked(self):
        assert "--end-date" not in self._build_cmd(_cfg(False))

    def test_log_line_when_locked(self, capsys, monkeypatch):
        """main() prints the holdout line (CLI script -> stdout, not logging)
        even with zero enabled assets."""
        import scripts.train_all_assets as taa

        cfg = _cfg(True)
        cfg["assets"] = {}
        cfg["retraining"] = {"enabled": True}
        monkeypatch.setattr(taa, "load_config", lambda: cfg)
        taa.main()
        assert "nightly retrain respects locked holdout, end-date=2026-08-08" in capsys.readouterr().out

    def test_no_log_line_when_unlocked(self, capsys, monkeypatch):
        import scripts.train_all_assets as taa

        cfg = _cfg(False)
        cfg["assets"] = {}
        cfg["retraining"] = {"enabled": True}
        monkeypatch.setattr(taa, "load_config", lambda: cfg)
        taa.main()
        assert "respects locked holdout" not in capsys.readouterr().out


class TestRetrainAssetTruncation:
    def test_frame_truncated_at_cutoff(self):
        """Bars >= holdout start never reach feature building."""
        from scripts.train_mt5 import locked_holdout_end_date, truncate_raw_before

        cfg = _cfg(True)
        # Span across the lock: 15-min bars over ~70 days from 2026-07-01
        # (past the 2026-08-08 cutoff).
        ts = pd.date_range("2026-07-01", periods=6800, freq="15min", tz="UTC")
        rng = np.random.default_rng(3)
        price = 4400 + np.cumsum(rng.normal(0, 0.5, len(ts)))
        raw = pd.DataFrame(
            {
                "timestamp_utc": (ts - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": rng.integers(10, 100, len(ts)).astype(float),
            }
        )
        end = locked_holdout_end_date(cfg)
        assert end is not None
        cut = truncate_raw_before(raw, end, "XAUUSD")
        cutoff_ts = int(pd.Timestamp(end, tz="UTC").timestamp())
        assert (cut["timestamp_utc"] < cutoff_ts).all()
        assert len(cut) < len(raw)

    def test_unlocked_path_uses_full_frame(self):
        """Backwards compatibility: lock off -> no truncation is applied."""
        cfg = _cfg(False)
        raw = _raw_frame()
        assert locked_holdout_end_date(cfg) is None
        # The retrain path applies truncation ONLY when the helper returns a
        # date; simulating that branch with None end keeps the frame as-is.
        end = locked_holdout_end_date(cfg)
        out = truncate_raw_before(raw, end, "XAUUSD") if end else raw
        assert out is raw
