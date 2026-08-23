# -*- coding: utf-8 -*-
"""Unit tests for the manual system (ТЗ). Run: python -m unittest challenge.manual.test_manual"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from challenge.manual import risk as risk_mod
from challenge.manual import scanner as scanner_mod
from challenge.manual import journal as journal_mod
from challenge.manual import outcomes as outcomes_mod

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CANDLES = os.path.join(ROOT, "data", "backtest", "candles")
WATCH = ["AAPL", "NVDA", "TSLA", "SPY", "GLD", "COIN", "AMD", "MU", "MRVL", "PLTR"]
DATES = [dt.date(2026, 7, 28), dt.date(2026, 7, 29), dt.date(2026, 7, 30),
         dt.date(2026, 7, 31), dt.date(2026, 8, 3), dt.date(2026, 8, 4),
         dt.date(2026, 8, 5), dt.date(2026, 8, 6), dt.date(2026, 8, 7),
         dt.date(2026, 8, 10), dt.date(2026, 8, 11), dt.date(2026, 8, 12),
         dt.date(2026, 8, 13), dt.date(2026, 8, 14), dt.date(2026, 8, 17),
         dt.date(2026, 8, 18), dt.date(2026, 8, 19)]


class TestRisk(unittest.TestCase):
    def test_profile_params(self):
        p = risk_mod.profile_params(1, "B", 0.0, 1000.0)
        self.assertEqual(p["risk_usd"], 2.5)
        # RESEARCH 2026-08-22: daily_limit raised $15→$25, profit_lock removed
        self.assertEqual(p["daily_limit_usd"], 25.0)
        self.assertEqual(p["max_trades"], 3)
        self.assertFalse(p["only_a"])   # grade A/B is not predictive — no gating
        self.assertEqual(p["profit_lock_usd"], 0.0)

    def test_drawdown_scaling(self):
        # Grade does not predict outcomes (24w data), so scaling reduces size
        # and trade count but never restricts to A-setups only.
        p = risk_mod.profile_params(1, "B", -50.0, 1000.0)   # -5%
        self.assertEqual(p["risk_usd"], 1.5)
        self.assertEqual(p["max_trades"], 1)
        self.assertFalse(p["only_a"])
        p2 = risk_mod.profile_params(1, "B", -20.0, 1000.0)  # -2%
        self.assertEqual(p2["risk_usd"], 2.5)

    def test_stop_day_after_two_losses(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            # After 1 loss: status still active (stop_after_losses=2 for B)
            sm.record_trade(-2.5)
            sm.update_equity(997.5)  # update equity to reflect the loss
            self.assertEqual(sm.state.status, "active")
            # After 2 losses: stop-day triggers
            sm.record_trade(-2.5)
            sm.update_equity(995.0)
            self.assertEqual(sm.state.status, "stop_day")
            ok, _ = sm.can_trade("A")
            self.assertFalse(ok)

    def test_profit_lock(self):
        # RESEARCH 2026-08-22: profit_lock disabled (usd=0) — test that +$25 does NOT flatten
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            # profit_lock_usd=0 means disabled — should NOT flatten
            self.assertEqual(sm.update_equity(1025.0), "trade")
            self.assertEqual(sm.state.status, "active")

    def test_pause(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 940.0, 1000.0, dt.datetime.now())
            self.assertEqual(sm.update_equity(940.0), "halt")
            self.assertEqual(sm.state.status, "paused")
            r = sm.start_day(1, "B", 940.0, 1000.0, dt.datetime.now())
            self.assertFalse(r["ok"])

    def test_violation_forces_stop_day(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            sm.record_trade(-2.5, violation="trading after stop-day")
            self.assertEqual(sm.state.status, "stop_day")
            self.assertIn("violation", sm.state.status_reason)

    def test_position_size(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            size = sm.position_size(300.0, 299.0, "long")   # $1 stop -> 2.5 shares
            self.assertAlmostEqual(size, 2.5, places=2)


class TestScanner(unittest.TestCase):
    def _candles(self, sym):
        p = os.path.join(CANDLES, sym + ".json")
        if not os.path.exists(p):
            self.skipTest(f"no candles for {sym}")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def test_resample(self):
        base = [{"time": 100, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1},
                {"time": 160, "open": 1.5, "high": 2.5, "low": 1, "close": 2, "volume": 2},
                {"time": 360, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 3}]
        out = scanner_mod.resample(base, 5)
        self.assertEqual(len(out), 1)  # 100..360 spans 5 min
        self.assertEqual(out[0]["high"], 3)
        self.assertEqual(out[0]["low"], 0)
        self.assertEqual(out[0]["close"], 2.5)
        self.assertEqual(out[0]["volume"], 6)

    def test_ema(self):
        self.assertAlmostEqual(scanner_mod.ema([1, 2, 3, 4, 5], 3)[-1], 4.0625)

    def test_scan_full_watchlist_sane(self):
        tradable = 0
        inverted = 0
        for d in DATES:
            for sym in WATCH:
                candles = self._candles(sym)
                res = scanner_mod.scan_setup(sym, d, candles, dt.time(13, 30), {})
                if res.tradable:
                    tradable += 1
                    if res.bias == "long":
                        self.assertGreater(res.entry, res.stop, f"{d} {sym}")
                    else:
                        self.assertLess(res.entry, res.stop, f"{d} {sym}")
                    self.assertGreaterEqual(res.rr, 2.0)
                    self.assertIn(res.grade, ("A", "B"))
        self.assertGreaterEqual(tradable, 1)
        self.assertEqual(inverted, 0)

    @staticmethod
    def _synth_day(pin):
        """Synthetic 2026-08-20 session day (08:00-23:59 UTC, 960 1-min bars)
        with a fixed structure: steady uptrend, impulse at 14:00, ~40% pullback
        to ~102.8, then the signal bar given by `pin` (o, h, l, c) at 14:30.
        Lets tests control whether the signal closes above or below the stop."""
        import random as _random
        _random.seed(42)

        def _bar(ts, o, h, l, c, v):
            return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}

        start = dt.datetime(2026, 8, 20, 8, 0, tzinfo=dt.timezone.utc)
        out, price = [], 100.0
        for i in range(960):
            ts = int((start + dt.timedelta(minutes=i)).timestamp())
            o = price
            c = o + 0.006 + _random.uniform(-0.01, 0.01)
            h = max(o, c) + _random.uniform(0.005, 0.03)
            l = min(o, c) - _random.uniform(0.005, 0.03)
            out.append(_bar(ts, round(o, 4), round(h, 4), round(l, 4), round(c, 4),
                            round(_random.uniform(800, 1200))))
            price = c

        def _override(t_from, t_to, o, h, l, c, v):
            s = dt.datetime(2026, 8, 20, t_from[0], t_from[1], tzinfo=dt.timezone.utc)
            e = dt.datetime(2026, 8, 20, t_to[0], t_to[1], tzinfo=dt.timezone.utc)
            n = int((e - s).total_seconds() // 60)
            o0 = o
            for i in range(n):
                ts = int((s + dt.timedelta(minutes=i)).timestamp())
                op = o0 + (c - o0) * (i / n)
                hi = max(op, h) + 0.001
                lo = min(op, l) - 0.001
                for b in out:
                    if b["time"] == ts:
                        b["open"] = round(op, 4)
                        b["high"] = round(hi, 4)
                        b["low"] = round(lo, 4)
                        b["close"] = round(op, 4)
                        b["volume"] = v
                        break

        _override((14, 0), (14, 5), 101.6, 103.6, 101.6, 103.6, 8000)      # impulse
        _override((14, 5), (14, 30), 103.6, 102.8, 103.6, 102.8, 1500)     # pullback 40%
        _override((14, 30), (14, 35), pin[0], pin[1], pin[2], pin[3], 1500)  # signal bar
        pc = pin[3]
        _override((14, 35), (16, 0), pc, pc + 1.0, pc + 1.4, pc, 1200)     # recovery
        return out

    def test_degenerate_levels_rejected(self):
        """A signal closing BELOW the stop (long: stop > entry) must be NO-GO
        and never reach alerts — regression for the MSTR/IONQ/LCID cases."""
        day = self._synth_day((102.8, 102.9, 101.9, 102.6))  # bearish pin below stop
        res = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), day, dt.time(13, 30), {})
        self.assertIn(res.grade, ("A", "B"))        # pre-fix this was tradable
        self.assertFalse(res.tradable)
        self.assertEqual(res.bias, "none")
        self.assertIn("degenerate levels", res.no_go)
        self.assertLess(res.entry, res.stop)          # the exact inverted levels rejected

    def test_sane_levels_still_tradable(self):
        """Same structure with the signal closing ABOVE the stop stays tradable
        (guards against over-rejection)."""
        day = self._synth_day((102.85, 103.3, 102.4, 103.0))  # bullish pin, sane levels
        res = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), day, dt.time(13, 30), {})
        self.assertTrue(res.tradable)
        self.assertEqual(res.bias, "long")
        self.assertGreater(res.entry, res.stop)
        self.assertGreaterEqual(res.rr, 2.0)

    def test_target_rr_config(self):
        """target_rr из конфига: тейк и rr растягиваются до 3.5R (live-план)."""
        day = self._synth_day((102.85, 103.3, 102.4, 103.0))  # bullish pin, sane levels
        res = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), day, dt.time(13, 30),
                                     {"target_rr": 3.5})
        self.assertTrue(res.tradable)
        risk = res.entry - res.stop
        self.assertAlmostEqual(res.target, res.entry + 3.5 * risk, places=3)
        self.assertAlmostEqual(res.rr, 3.5, places=2)

    @staticmethod
    def _two_days(amp2):
        """Two full days (2026-08-19 normal, 2026-08-20 'today'), 08:00-23:59 UTC.
        amp2 scales today's per-bar moves vs the fixed day-1 amplitude -> controls
        the activity ratio today/normal (amp2=1.0 => ratio ~1, amp2=0.1 => ~0.1)."""
        import random as _random
        _random.seed(7)

        def _day(d, amp):
            start = dt.datetime(d.year, d.month, d.day, 8, 0, tzinfo=dt.timezone.utc)
            out, price = [], 100.0
            for i in range(960):
                ts = int((start + dt.timedelta(minutes=i)).timestamp())
                o = price
                c = o + amp * 0.05 + _random.uniform(-amp * 0.06, amp * 0.06)
                h = max(o, c) + amp * 0.05
                l = min(o, c) - amp * 0.05
                out.append({"time": ts, "open": round(o, 4), "high": round(h, 4),
                            "low": round(l, 4), "close": round(c, 4), "volume": 1000})
                price = c
            return out

        return _day(dt.date(2026, 8, 19), 1.0), _day(dt.date(2026, 8, 20), amp2)

    def test_signal_dead_zone_rejected(self):
        """Сигнал в мёртвой зоне 60-69 мин (конфиг signal_dead_zone) должен быть
        NO-GO — регрессия фильтра качества (24w: единственный отрицательный
        бакет по времени сигнала, avgR −0.324 n=44)."""
        day = self._synth_day((102.85, 103.3, 102.4, 103.0))  # bullish pin, sane levels
        # без фильтра — tradable; сигнал должен попасть в 60-69 мин
        base = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), day, dt.time(13, 30), {})
        self.assertTrue(base.tradable)
        sig_ts = base.signal_bar["time"]
        sess0 = dt.datetime(2026, 8, 20, 13, 30, tzinfo=dt.timezone.utc).timestamp()
        sig_min = (sig_ts - sess0) / 60.0
        self.assertTrue(60.0 <= sig_min <= 69.0,
                        f"сигнал на {sig_min:.1f} мин — тест ждёт мёртвую зону 60-69")
        res = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), day, dt.time(13, 30),
                                     {"signal_dead_zone": [60, 69]})
        self.assertFalse(res.tradable)
        self.assertIn("signal dead zone", ";".join(res.no_go))

    def test_dead_day_rejected(self):
        """A low-activity day (range << 70% of the prior session) must be NO-GO
        even if the setup would otherwise form — regression for the atr>=0.7
        dead-day filter (24w data: ~100% of losses live on such days)."""
        d1, d2 = self._two_days(0.1)                 # today ~10% of normal range
        res = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), d1 + d2,
                                     dt.time(13, 30), {})
        self.assertFalse(res.tradable)
        self.assertIn("abnormal daily ATR", res.no_go)

    def test_normal_day_passes(self):
        """Same two-day structure with a normal-range today must NOT trip the
        activity filter (guards against over-rejection on live sessions)."""
        d1, d2 = self._two_days(1.0)                 # today ~ normal range
        res = scanner_mod.scan_setup("SYN", dt.date(2026, 8, 20), d1 + d2,
                                     dt.time(13, 30), {})
        self.assertNotIn("abnormal daily ATR", res.no_go)


class TestOutcomes(unittest.TestCase):
    @staticmethod
    def _ts(h, m):
        return int(dt.datetime(2026, 8, 20, h, m, tzinfo=dt.timezone.utc).timestamp())

    @staticmethod
    def _bar(ts, o, h, l, c):
        return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100}

    def test_stop(self):
        sig = self._ts(14, 30)
        bars = [self._bar(sig + 60, 100.0, 100.4, 99.9, 100.0),
                self._bar(sig + 120, 100.0, 100.2, 99.4, 99.5)]
        out, r, _ = outcomes_mod.simulate_outcome(sig, 100.0, 99.5, 101.0, "long", bars)
        self.assertEqual(out, "stop")
        self.assertEqual(r, -1.0)

    def test_target_full_position(self):
        # вся позиция едет до тейка +2R, без частичной фиксации на 1R
        sig = self._ts(14, 30)
        bars = [self._bar(sig + 60, 100.0, 100.5, 100.2, 100.4),   # +1R — не выход
                self._bar(sig + 120, 100.4, 101.1, 100.8, 101.0)]  # +2R — тейк
        out, r, _ = outcomes_mod.simulate_outcome(sig, 100.0, 99.5, 101.0, "long", bars)
        self.assertEqual(out, "target")
        self.assertEqual(r, 2.0)

    def test_target_short(self):
        sig = self._ts(14, 30)
        # шорт: вход 100, стоп 100.5, тейк 99.0 (2R вниз)
        bars = [self._bar(sig + 60, 100.0, 100.3, 99.8, 99.9),
                self._bar(sig + 120, 99.9, 100.1, 98.9, 99.0)]
        out, r, _ = outcomes_mod.simulate_outcome(sig, 100.0, 100.5, 99.0, "short", bars)
        self.assertEqual(out, "target")
        self.assertEqual(r, 2.0)

    def test_pending_then_eod(self):
        sig = self._ts(14, 30)
        bars = [self._bar(sig + 60, 100.0, 100.3, 99.9, 100.0)]   # ни стопа, ни тейка
        # сессия ещё идёт -> pending
        out, _, _ = outcomes_mod.simulate_outcome(sig, 100.0, 99.5, 101.0, "long", bars,
                                                  now_ts=self._ts(15, 0))
        self.assertIsNone(out)
        # после 19:55 -> EOD по цене закрытия (= входу -> R=0)
        out, r, _ = outcomes_mod.simulate_outcome(sig, 100.0, 99.5, 101.0, "long", bars,
                                                  now_ts=self._ts(20, 0))
        self.assertEqual(out, "eod")
        self.assertAlmostEqual(r, 0.0, places=3)

    def test_stats_aggregates(self):
        rows = [
            {"grade": "A", "r": "-1.0"},
            {"grade": "A", "r": "1.5"},
            {"grade": "B", "r": "0.5"},
            {"grade": "B", "r": "-1.0"},
            {"grade": "B", "r": "0.0"},
            {"grade": "", "r": ""},
        ]
        s = outcomes_mod.compute_stats(rows)
        self.assertEqual(s["A"]["n"], 2)
        self.assertEqual(s["A"]["wins"], 1)
        self.assertAlmostEqual(s["A"]["avg_r"], 0.25)
        self.assertEqual(s["B"]["n"], 3)
        self.assertEqual(s["B"]["flat"], 1)
        self.assertEqual(s["total"]["n"], 5)
        self.assertAlmostEqual(s["total"]["sum_r"], 0.0, places=3)

    def test_journal_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "outcomes.csv")
            outcomes_mod.append_journal(p, {"date": "2026-08-20", "symbol": "COIN",
                                            "grade": "B", "bias": "long",
                                            "signal_utc": "123", "entry": "151.5",
                                            "stop": "149.8", "target": "157.0",
                                            "rr": "3.5", "outcome": "target", "r": 3.5,
                                            "minutes": 45,
                                            "resolved_utc": "2026-08-20T19:56:00+00:00"})
            rows = outcomes_mod.read_journal(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "COIN")
            self.assertEqual(rows[0]["outcome"], "target")
            self.assertEqual(rows[0]["r"], "3.5")


class TestClusterCapAndEarnings(unittest.TestCase):
    """2026-08-23: anti-correlation cluster cap + earnings blackout."""

    def test_cluster_cap_blocks_second_same_cluster(self):
        with tempfile.TemporaryDirectory() as td:
            sm = risk_mod.DailyStateMachine(state_path=os.path.join(td, "s.json"))
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            ok, _ = sm.can_trade("B", cluster="crypto_beta")
            self.assertTrue(ok)
            # First trade in the cluster consumes the day's cluster slot.
            sm.record_trade(-2.5, cluster="crypto_beta")   # stop-day anyway (2nd rule is 2 losses; 1st loss ok)
            ok2, reason = sm.can_trade("B", cluster="crypto_beta")
            self.assertFalse(ok2)
            self.assertIn("кластер", reason)

    def test_earnings_blackout_window(self):
        from challenge.manual.scanner import earnings_blackout
        cal = {"2026-08-27": ["NVDA"], "2026-08-20": "*"}
        d = dt.date(2026, 8, 27)
        blocked, src = earnings_blackout("NVDA", d, cal, block_days=2)      # report day
        self.assertTrue(blocked); self.assertEqual(src, "2026-08-27")
        blocked2, src2 = earnings_blackout("NVDA", d + dt.timedelta(days=1), cal, 2)  # gap day
        self.assertTrue(blocked2); self.assertEqual(src2, "2026-08-27")
        blocked3, _ = earnings_blackout("NVDA", d + dt.timedelta(days=2), cal, 2)     # window over
        self.assertFalse(blocked3)
        wildcard, wsrc = earnings_blackout("ANY", dt.date(2026, 8, 21), cal, 2)       # "*" next day
        self.assertTrue(wildcard); self.assertEqual(wsrc, "2026-08-20")


class TestJournal(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "j.csv")
            n1 = journal_mod.add_trade(p, "2026-08-19", "14:30", "AAPL", "L", "B",
                                       318.65, 312.30, 331.33, 2.5, 0.25)
            n2 = journal_mod.add_trade(p, "2026-08-19", "14:40", "NVDA", "S", "C",
                                       219, 220, 217, 2.5, 0.25)
            self.assertEqual((n1, n2), (1, 2))
            self.assertTrue(journal_mod.close_trade(p, 1, 5.0, 2.0, "W"))
            self.assertTrue(journal_mod.close_trade(p, 2, -2.5, -1.0, "L"))
            s = journal_mod.daily_summary(p)
            self.assertEqual(s[0]["trades"], 2)
            self.assertAlmostEqual(s[0]["pnl_usd"], 2.5)
            w = journal_mod.weekly_metrics(p)
            self.assertEqual(len(w), 1)
            self.assertAlmostEqual(w[0]["avg_r"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)