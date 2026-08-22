# -*- coding: utf-8 -*-
"""Pair signal monitor — runs as a background thread in the main forex trader.

Polls PairAnalyzer + SignalEngine every N minutes (configurable), sends Telegram
alerts when |z| > entry_z (default 2σ), and resolves open pair outcomes
(exit_z / stop_z / timeout). Independent of US session — crypto is 24/7.

Integration with control_bot.py:
    from alerts.pair_monitor import PairMonitor
    monitor = PairMonitor(send_fn)
    monitor.start()

The send_fn is control_bot._send(chat_id, text) — or any callable that
posts a Telegram message.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

import yaml

logger = logging.getLogger("pair_monitor")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIR_SENT_FILE = os.path.join(ROOT, "data", "manual", "pair_alerts_sent.json")
PAIR_RESOLVED_FILE = os.path.join(ROOT, "data", "manual", "pair_outcomes_resolved.json")
PAIR_JOURNAL_CSV = os.path.join(ROOT, "data", "manual", "pair_journal.csv")
PAIR_STATS_FILE = os.path.join(ROOT, "data", "manual", "pair_outcomes_stats.json")


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class PairMonitor:
    """Background pair-signal monitor for the main forex system.

    Parameters
    ----------
    send_fn : callable(chat_id, text, parse_mode="")
        Telegram send function (typically control_bot._send).
    admin_chat_id : str
        Chat to send pair alerts to.
    config_path : str, optional
        Path to manual_config.yaml (for pair_alerts section).
    pairs_config_path : str, optional
        Path to pairs_config.yaml (for pair analysis config).
    """

    def __init__(
        self,
        send_fn: Callable,
        admin_chat_id: str = "",
        config_path: str = "",
        pairs_config_path: str = "",
    ):
        self.send_fn = send_fn
        self.admin_chat_id = admin_chat_id

        # Load configs
        if not config_path:
            config_path = os.path.join(ROOT, "challenge", "manual", "manual_config.yaml")
        if not pairs_config_path:
            pairs_config_path = os.path.join(ROOT, "config", "pairs_config.yaml")

        try:
            with open(config_path, encoding="utf-8") as f:
                self.cfg = yaml.safe_load(f) or {}
        except Exception:
            self.cfg = {}

        try:
            with open(pairs_config_path, encoding="utf-8") as f:
                self.pairs_cfg = yaml.safe_load(f) or {}
        except Exception:
            self.pairs_cfg = {}

        pa_cfg = self.cfg.get("pair_alerts", {})
        self.enabled = pa_cfg.get("enabled", False)
        self.pairs_to_watch = [n.lower() for n in pa_cfg.get("pairs", [])]
        self.timeframe = pa_cfg.get("timeframe", "H1")
        self.poll_minutes = int(pa_cfg.get("poll_minutes", 55))
        self.cooldown_hours = int(pa_cfg.get("alert_cooldown_hours", 23))

        # Lazy imports (heavy modules)
        self._pair_analyzer = None
        self._signal_engine = None
        self._ensemble_engine = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_check = 0.0

    def _ensure_imports(self):
        if self._pair_analyzer is not None:
            return
        from pairs_analysis import PairAnalyzer, SignalEngine, EnsembleEngine
        from pairs_analysis import load_config as load_pairs_config

        # Reload pairs_config to get fresh thresholds
        if not self.pairs_cfg:
            self.pairs_cfg = load_pairs_config()

        thresholds = self.pairs_cfg.get("thresholds", {})
        analysis = self.pairs_cfg.get("analysis", {})
        bt_cfg = dict(analysis)
        bt_cfg.update(self.pairs_cfg.get("backtest") or {})

        self._pair_analyzer = PairAnalyzer
        self._signal_engine = SignalEngine(thresholds, bt_cfg)
        self._ensemble_engine = EnsembleEngine(self.pairs_cfg)

    def _format_alert(self, sig, m, ensemble=None) -> str:
        side_ru = "LONG" if sig.direction == "long" else "SHORT"
        side_emoji = "\U0001f7e2" if sig.direction == "long" else "\U0001f534"
        p1, p2 = m.name.split("/")
        rec = (f"long {p1} / short {p2}" if sig.direction == "long"
               else f"short {p1} / long {p2}")

        # z-score strength indicator
        z_abs = abs(sig.z)
        if z_abs >= 3.0:
            z_power = "\u26a1 EXTREME"
        elif z_abs >= 2.5:
            z_power = "\U0001f525 STRONG"
        else:
            z_power = "\U0001f4a1 NORMAL"

        # Build ensemble section
        ens_section = ""
        if ensemble is not None:
            ens_summary = ensemble.summary_line()
            ens_conf = ensemble.confidence
            top = sorted(ensemble.engines, key=lambda e: e.confidence,
                         reverse=True)[:4]
            engine_lines = []
            for e in top:
                dir_label = {"long": "LONG", "short": "SHORT",
                             "neutral": "NEUTRAL"}.get(e.direction, e.direction.upper())
                engine_lines.append(
                    f"  {e.name:<14} {dir_label:>8}  {e.confidence:>4.0f}%")
            engines_block = "\n".join(engine_lines)
            ens_section = (
                f"\n\U0001f9e0 ENSEMBLE\n"
                f"{ens_summary} ({ens_conf:.0f}% confidence)\n"
                f"{engines_block}")

        # Period label
        hl_label = f"{m.half_life_days:.1f}d"
        if m.half_life_days < 0.1:
            hl_label = f"{m.half_life_days * 24:.0f}h"

        hurst_label = "\u2193 MR" if m.hurst < 0.5 else "\u2191 Trend"

        return (
            f"{'─' * 30}\n"
            f"{side_emoji} {side_ru} {m.name}\n"
            f"{'─' * 30}\n"
            f"σ {sig.z:+.2f}  ({z_power})\n"
            f"Entry: σ = 2.0 | Target: σ = 0.0\n"
            f"Stop: |σ| > 3.0\n"
            f"\n"
            f"\U0001f4ca {m.name} Parameters\n"
            f"  β Kalman    {m.beta:.2f}\n"
            f"  Ratio       {m.ratio:.2f}\n"
            f"  Half-life   {hl_label}\n"
            f"  ADF p-val   {m.adf_p:.4f}\n"
            f"  Hurst       {m.hurst:.2f}  {hurst_label}\n"
            f"\n"
            f"\U0001f4a8 Plan: {rec}\n"
            f"\U0001f552 Timeout: {2 * m.half_life_days:.1f}d"
            f"{ens_section}\n"
            f"{'─' * 30}"
        )

    def _check_signals(self) -> list:
        """Check all pairs for mean-reversion signals. Returns list of
        (pair_name, text, metadata) for new alerts."""
        if not self.enabled:
            return []

        self._ensure_imports()

        pair_sent = _load_json(PAIR_SENT_FILE)
        now = dt.datetime.now(dt.timezone.utc)
        alerts = []

        for pair in self.pairs_cfg.get("pairs", []):
            name = pair["name"]
            if self.pairs_to_watch and name.lower() not in self.pairs_to_watch:
                continue

            # Cooldown check
            last_sent = pair_sent.get(name, {}).get("sent_at")
            if last_sent:
                try:
                    last_dt = dt.datetime.fromisoformat(last_sent)
                    if (now - last_dt).total_seconds() < self.cooldown_hours * 3600:
                        continue
                except Exception:
                    pass

            try:
                pa = self._pair_analyzer(pair, self.pairs_cfg.get("analysis", {}))
                m = pa.analyze(self.timeframe)
                sig = self._signal_engine.current(m)
                if sig.valid:
                    try:
                        ensemble = self._ensemble_engine.forecast(m)
                    except Exception:
                        ensemble = None
                    text = self._format_alert(sig, m, ensemble)
                    metadata = {
                        "pair_name": name,
                        "direction": sig.direction,
                        "entry_z": sig.z,
                        "half_life_days": m.half_life_days,
                        "beta": m.beta,
                        "adf_p": m.adf_p,
                        "hurst": m.hurst,
                        "sigma": m.sigma,
                        "regime": "mean-reverting" if m.hurst < 0.5 else "trending",
                        "ensemble_direction": (ensemble.direction if ensemble
                                               else "neutral"),
                        "ensemble_confidence": (ensemble.confidence if ensemble
                                                else 0),
                        "ensemble_line": (ensemble.summary_line() if ensemble
                                          else ""),
                    }
                    alerts.append((name, text, metadata))
            except Exception as e:
                logger.warning("pair signal %s: %s", name, e)

        return alerts

    def _resolve_outcomes(self):
        """Check open pair positions for exit_z / stop_z / timeout."""
        try:
            from challenge.manual.pair_outcomes import resolve_pair_outcomes
            resolve_pair_outcomes(self.timeframe)
        except Exception as e:
            logger.warning("pair outcome resolve: %s", e)

    def _poll_once(self):
        """One poll cycle: check signals, resolve outcomes."""
        if not self.enabled:
            return

        # Check pair signals
        alerts = self._check_signals()
        pair_sent = _load_json(PAIR_SENT_FILE)
        now = dt.datetime.now(dt.timezone.utc)

        for pair_name, text, meta in alerts:
            try:
                self.send_fn(self.admin_chat_id, text)
                rec = {"sent_at": now.isoformat(), **meta}
                pair_sent[pair_name] = rec
                _save_json(PAIR_SENT_FILE, pair_sent)
                logger.info("pair alert sent for %s", pair_name)
            except Exception as e:
                logger.warning("pair alert send failed: %s", e)

        # Resolve outcomes
        self._resolve_outcomes()

    def _loop(self):
        poll_sec = self.poll_minutes * 60
        logger.info("Pair monitor started: tf=%s, poll=%dm, pairs=%s",
                     self.timeframe, self.poll_minutes,
                     self.pairs_to_watch or "all")
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - self._last_check >= poll_sec:
                    self._last_check = now
                    self._poll_once()
            except Exception as e:
                logger.warning("pair monitor error: %s", e)
            self._stop.wait(min(30, poll_sec))  # wake up every 30s or poll interval

    def start(self):
        """Start the background monitoring thread."""
        if not self.enabled:
            logger.info("Pair monitor disabled (pair_alerts.enabled=false)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="pair-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background monitoring thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # --- Synchronous query (for /pairs command) ---
    def query_all(self) -> str:
        """Synchronous query of all pairs. Returns formatted text for Telegram."""
        self._ensure_imports()
        lines = [f"\U0001f4ca PAIRS — {self.timeframe}\n"]
        for pair in self.pairs_cfg.get("pairs", []):
            name = pair["name"]
            if self.pairs_to_watch and name.lower() not in self.pairs_to_watch:
                continue
            try:
                pa = self._pair_analyzer(pair, self.pairs_cfg.get("analysis", {}))
                m = pa.analyze(self.timeframe)
                sig = self._signal_engine.current(m)
                forecast = self._ensemble_engine.forecast(m)

                icon = {"long": "\U0001f7e2 LONG", "short": "\U0001f534 SHORT"}.get(
                    sig.direction, "\u26aa NO EDGE")
                adf_icon = "\u2705" if m.adf_p < 0.05 else "\u274c"
                hurst_icon = "\u2705" if m.hurst < 0.5 else "\u274c"

                lines.append(
                    f"{name} [{m.n_bars} bars]\n"
                    f"  z: {sig.z:+.3f}\u03c3 | \u03b2: {m.beta:.2f} | "
                    f"ratio: {m.ratio:.1f}\n"
                    f"  {adf_icon} ADF p={m.adf_p:.4f} | "
                    f"{hurst_icon} H={m.hurst:.2f} | "
                    f"HL={m.half_life_days:.1f}d\n"
                    f"  Signal: {icon}\n"
                    f"  Ensemble: \u2192 {forecast.direction.upper()} "
                    f"CONF {forecast.confidence:.0f}%"
                )
            except Exception as e:
                lines.append(f"{name}: ERROR {e}")

        # Pair stats if available
        try:
            from challenge.manual.outcomes import read_journal, compute_stats
            rows = read_journal(PAIR_JOURNAL_CSV)
            stats = compute_stats(rows)
            if stats.get("total", 0) > 0:
                lines.append(
                    f"\n\U0001f4c8 Pair stats: {stats['total']} trades, "
                    f"WR {stats.get('win_rate', 0):.0f}%, "
                    f"avgR {stats.get('avg_r', 0):+.2f}")
        except Exception:
            pass

        return "\n".join(lines)
