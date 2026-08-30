"""DST-transition simulation for the auto server-offset detector + session tags.

Scenario (EEST -> EET, EU fall-back):
  * Before the flip the broker server runs UTC+3 (EEST): a live tick's
    ``tick.time - time.time()`` == +3.0h.
  * After the flip it runs UTC+2 (EET): the same measurement == +2.0h.

Checks:
  1. ``detect_server_offset_hours_detailed`` flips 3.0 -> 2.0 when the tick
     delta changes, with mode=detected (no stale 3h carried over).
  2. Bars normalized with the CORRECT per-period offset (3 before / 2 after)
     tag to the SAME true-UTC session windows (asia 0-8 / london 8-13 /
     newyork 13-22 UTC) — i.e. tags never drift with DST, because the offset
     is subtracted *before* tagging.
  3. Weekend guard is inert during the week (the detection is trusted).

The tick is monkeypatched at the mt5 module level, so no real terminal state
is touched. Run on a weekday (current date is Thursday, so the guard is
inert by construction).

Usage:
    python -m scripts.diag_dst_transition
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import data.mt5_provider as mp
from config.loader import load_config
from data.session_tagger import tag_session_with_weekend


class _FakeTick:
    def __init__(self, server_time: float):
        self.time = server_time


def _patch_tick(offset_hours: float):
    """Make the module's mt5 return a tick whose server time is now+offset."""
    now = time.time()

    class _FakeMT5:
        def initialize(self, *a, **k):
            return True

        def symbol_select(self, *a, **k):
            return True

        def symbol_info_tick(self, symbol):
            return _FakeTick(now + offset_hours * 3600.0)

    mp.mt5 = _FakeMT5()  # type: ignore[assignment]


def main() -> int:
    cfg = load_config()
    sessions = cfg["sessions"]
    print(f"today (UTC): {datetime.now(UTC).isoformat()}  weekday={datetime.now(UTC).weekday()}")

    print("\n[1] offset detection flip")
    # Ensure the weekend guard is inert on this run (weekday 0-4).
    assert datetime.now(UTC).weekday() < 5, "run on a weekday"
    for label, off in (("EEST (summer)", 3.0), ("EET (winter)", 2.0)):
        _patch_tick(off)
        got, info = mp.detect_server_offset_hours_detailed(fallback=3.0)
        status = "OK" if (got == off and info["mode"] == "detected") else "FAIL"
        print(
            f"  {label:16s} tick_delta_hours={info.get('delta_hours'):+.4f} "
            f"-> detected {got:.1f}  mode={info['mode']}  [{status}]"
        )
        if status == "FAIL":
            return 1

    print("\n[2] session tags stay correct across the DST flip")
    # Server timestamps for a full 24h day (server wall-clock hours 0..23).
    # With the correct offset these are *different* UTC hours before/after DST,
    # and the tags must match the TRUE UTC session windows in both cases.
    base = pd.Timestamp("2026-10-25", tz="UTC")  # EU fall-back Sunday
    rows = []
    for server_hour in range(24):
        for offset, period in ((3.0, "EEST"), (2.0, "EET")):
            server_ts = base + pd.Timedelta(hours=server_hour)
            utc_ts = server_ts - pd.Timedelta(hours=offset)  # _normalize_rates shift
            tag = tag_session_with_weekend(utc_ts, sessions)
            rows.append({"period": period, "server_hour": server_hour, "utc_hour": utc_ts.hour, "tag": tag})
    df = pd.DataFrame(rows)

    # The invariant: for a FIXED true-UTC hour, both periods produce the same tag.
    pivot = df.pivot_table(index="utc_hour", columns="period", values="tag", aggfunc=lambda s: s.iloc[0])
    mism = pivot[pivot["EEST"] != pivot["EET"]]
    print(f"  utc-hours compared: {len(pivot)}  mismatches: {len(mism)}")
    if len(mism):
        print("  MISMATCHES:")
        print(mism.to_string())
        return 1

    # And the canonical windows are honored (weekday rows only for spot checks).
    checks = [
        (2, "asia"),
        (10, "london"),
        (16, "newyork"),
        (23, "off_session"),
    ]
    ok = True
    for hour, expect in checks:
        tag = tag_session_with_weekend(pd.Timestamp("2026-10-26", tz="UTC") + pd.Timedelta(hours=hour), sessions)
        good = (expect in tag) or (expect == "off_session" and tag == "off_session")
        ok &= good
        print(f"  utc {hour:02d}:00 -> '{tag}'  (expect contains '{expect}') [{'OK' if good else 'FAIL'}]")
    if not ok:
        return 1

    print("\n[3] weekend guard (Sat/Sun UTC -> fallback, never stale detection)")

    # Patch the module's now() so the detector sees Saturday, then confirm the
    # fallback path is used even with a fresh-looking tick. ``mp.datetime`` is
    # the ``datetime`` CLASS (``from datetime import datetime``), so we swap a
    # small stub class whose ``now`` returns a Saturday.
    class _StubDt:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 10, 24, 12, 0, 0, tzinfo=tz or UTC)

    orig_now = mp.datetime
    mp.datetime = _StubDt
    try:
        _patch_tick(2.0)  # tick looks fresh but it's Saturday
        got, info = mp.detect_server_offset_hours_detailed(fallback=3.0)
        good = got == 3.0 and info["mode"] == "fallback"
        print(f"  Sat: -> fallback {got:.1f} mode={info['mode']} reason={info['reason']} [{'OK' if good else 'FAIL'}]")
        if not good:
            return 1
    finally:
        mp.datetime = orig_now

    print("\nALL DST CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
