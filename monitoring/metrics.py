"""ТЗ 6.1: execution metrics collection.

``MetricsCollector`` is a thread-safe, in-memory counter/timing registry for
the execution pipeline (groups created, submissions, fills, rejections with
reason codes, per-stage timings, poll durations) plus a JSONL history writer
(``logs/metrics.jsonl``) with size-based rotation.

Design notes:
    * Pure stdlib, no dependencies on the trading code — the executor
      instruments itself by calling ``record_*`` methods.
    * All aggregates computed on demand in ``summary()``; individual records
      are kept in bounded deques so memory cannot grow unbounded.
    * The JSONL sink is optional (``jsonl_path=None`` disables persistence)
      and fail-open: a broken file sink must never break execution.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("monitoring.metrics")

METRICS_SINK_MAX_BYTES = 10 * 1024 * 1024  # P2-33-aligned rotation size
METRICS_SINK_BACKUPS = 3


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return float(ordered[idx])


class MetricsCollector:
    """Thread-safe counters + timing buffers for the execution pipeline."""

    def __init__(
        self,
        *,
        history: int = 1000,
        poll_window: int = 100,
        jsonl_path: str | None = None,
        sink_max_bytes: int = METRICS_SINK_MAX_BYTES,
        sink_backups: int = METRICS_SINK_BACKUPS,
        stage_history: int = 1000,
        clock: Callable[[], float] = time.time,
    ):
        self._lock = threading.Lock()
        self._clock = clock
        self.started_at = clock()

        # --- counters (ТЗ 6.1 JSON shape) --------------------------------
        self._counters: dict[str, int] = {
            "groups_created": 0,
            "orders_sent": 0,
            "orders_filled": 0,
            "orders_partial": 0,
            "orders_rejected": 0,
            "polls": 0,
        }
        self._reject_reasons: dict[str, int] = {}

        # --- bounded timing buffers ---------------------------------------
        self._poll_durations_ms: deque[float] = deque(maxlen=poll_window)
        self._mt5_calls_per_poll: deque[int] = deque(maxlen=poll_window)
        self._stage_history = max(1, int(stage_history))
        self._stage_timings_ms: dict[str, deque[float]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=history)

        # --- JSONL sink (append + size rotation) ---------------------------
        self._jsonl_path = jsonl_path
        self._sink_max_bytes = max(1, int(sink_max_bytes))
        self._sink_backups = max(0, int(sink_backups))
        self._sink_lock = threading.Lock()

    # ------------------------------------------------------------------ rec

    def record(self, metric: str, value: int = 1, **extra: Any) -> None:
        """Record one event: counters, rejection reasons and history entry."""
        with self._lock:
            if metric.startswith("rejected:"):
                reason = metric.split(":", 1)[1]
                self._counters["orders_rejected"] += value
                self._reject_reasons[reason] = self._reject_reasons.get(reason, 0) + value
            else:
                self._counters[metric] = self._counters.get(metric, 0) + value
            entry = {"ts": self._clock(), "metric": metric, "value": value}
            if extra:
                entry.update(extra)
            self._history.append(entry)

    def record_timing(self, stage: str, duration_ms: float, **extra: Any) -> None:
        """Record a per-stage duration (ms)."""
        with self._lock:
            buf = self._stage_timings_ms.setdefault(
                stage, deque(maxlen=self._stage_history))
            buf.append(float(duration_ms))
            entry = {"ts": self._clock(), "metric": "timing", "stage": stage,
                     "duration_ms": float(duration_ms)}
            if extra:
                entry.update(extra)
            self._history.append(entry)

    def record_poll(self, duration_ms: float, mt5_calls: int = 0) -> None:
        """Record one poll_once pass (duration + MT5 call count)."""
        with self._lock:
            self._counters["polls"] += 1
            self._poll_durations_ms.append(float(duration_ms))
            self._mt5_calls_per_poll.append(int(mt5_calls))
        self.record("poll_completed", duration_ms=round(float(duration_ms), 3))

    # ------------------------------------------------------------- snapshot

    def summary(self) -> dict[str, Any]:
        """Aggregate snapshot (the /api/execution-metrics payload body)."""
        with self._lock:
            orders_sent = self._counters.get("orders_sent", 0)
            orders_filled = self._counters.get("orders_filled", 0)
            rejected = self._counters.get("orders_rejected", 0)
            polls = self._counters.get("polls", 0)
            poll_durations = list(self._poll_durations_ms)
            mt5_calls = list(self._mt5_calls_per_poll)
            stages = {
                stage: {
                    "count": len(buf),
                    "avg_ms": round(sum(buf) / len(buf), 3) if buf else 0.0,
                    "p95_ms": round(_percentile(list(buf), 95), 3),
                }
                for stage, buf in self._stage_timings_ms.items()
            }
            counters = dict(self._counters)
            reject_reasons = dict(self._reject_reasons)
            started_at = self.started_at

        fill_rate = (100.0 * orders_filled / orders_sent) if orders_sent else 0.0
        return {
            **counters,
            "fill_rate_pct": round(fill_rate, 2),
            "reject_reasons": reject_reasons,
            "poll_count": polls,
            "poll_p95_ms": round(_percentile(poll_durations, 95), 3),
            "poll_avg_ms": (
                round(sum(poll_durations) / len(poll_durations), 3)
                if poll_durations else 0.0
            ),
            "mt5_calls_avg": (
                round(sum(mt5_calls) / len(mt5_calls), 2) if mt5_calls else 0.0
            ),
            "stages": stages,
            "uptime_s": round(self._clock() - started_at, 3),
        }

    # ------------------------------------------------------------ jsonl sink

    def flush_summary(self) -> dict[str, Any] | None:
        """Append the current summary as one JSONL line (fail-open).

        Rotation: when the sink exceeds ``sink_max_bytes`` it is renamed to
        ``<path>.1`` (older rotations shift; ``.<backups>`` is dropped) — the
        simple rotation scheme from P2-33, without importing logging handlers
        so the sink format stays pure JSONL.
        """
        if not self._jsonl_path:
            return None
        payload = {"ts": self._clock(), **self.summary()}
        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self._sink_lock:
                self._rotate_if_needed()
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            logger.warning("metrics sink write failed: %s", exc)
            return None
        return payload

    def _rotate_if_needed(self) -> None:
        path = self._jsonl_path
        if not path or not os.path.exists(path):
            return
        if os.path.getsize(path) < self._sink_max_bytes:
            return
        # shift .1 -> .2 -> ... and rotate the live file into .1
        for i in range(self._sink_backups - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.exists(src):
                if i + 1 > self._sink_backups:
                    os.remove(src)
                else:
                    os.replace(src, dst)
        if self._sink_backups > 0:
            os.replace(path, f"{path}.1")
        else:
            os.remove(path)


# ------------------------------------------------------------------ wiring ---

_collector: MetricsCollector | None = None
_collector_lock = threading.Lock()


def get_collector() -> MetricsCollector:
    """Process-wide collector singleton (jsonl sink per METRICS_JSONL env)."""
    global _collector
    with _collector_lock:
        if _collector is None:
            path = os.environ.get("METRICS_JSONL_PATH", "logs/metrics.jsonl")
            _collector = MetricsCollector(jsonl_path=path or None)
        return _collector


def set_collector(collector: MetricsCollector | None) -> None:
    """Test/setup hook: replace or clear the process-wide collector."""
    global _collector
    with _collector_lock:
        _collector = collector
