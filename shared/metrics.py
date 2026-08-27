"""Prometheus metrics registry and collectors (P2-12)."""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple


class Counter:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def get(self, **labels: str) -> float:
        key = tuple(sorted(labels.items()))
        with self._lock:
            return self._values.get(key, 0.0)


class Gauge:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = float(value)

    def get(self, **labels: str) -> float:
        key = tuple(sorted(labels.items()))
        with self._lock:
            return self._values.get(key, 0.0)


class MetricsRegistry:
    def __init__(self):
        self.counters: Dict[str, Counter] = {}
        self.gauges: Dict[str, Gauge] = {}

    def counter(self, name: str, description: str = "") -> Counter:
        if name not in self.counters:
            self.counters[name] = Counter(name, description)
        return self.counters[name]

    def gauge(self, name: str, description: str = "") -> Gauge:
        if name not in self.gauges:
            self.gauges[name] = Gauge(name, description)
        return self.gauges[name]

    def render_prometheus(self) -> str:
        lines: List[str] = []
        for name, c in sorted(self.counters.items()):
            if c.description:
                lines.append(f"# HELP {name} {c.description}")
            lines.append(f"# TYPE {name} counter")
            with c._lock:
                if not c._values:
                    lines.append(f"{name} 0.0")
                for labels, val in sorted(c._values.items()):
                    lbl_str = ",".join(f'{k}="{v}"' for k, v in labels)
                    lbl_part = f"{{{lbl_str}}}" if lbl_str else ""
                    lines.append(f"{name}{lbl_part} {val}")

        for name, g in sorted(self.gauges.items()):
            if g.description:
                lines.append(f"# HELP {name} {g.description}")
            lines.append(f"# TYPE {name} gauge")
            with g._lock:
                if not g._values:
                    lines.append(f"{name} 0.0")
                for labels, val in sorted(g._values.items()):
                    lbl_str = ",".join(f'{k}="{v}"' for k, v in labels)
                    lbl_part = f"{{{lbl_str}}}" if lbl_str else ""
                    lines.append(f"{name}{lbl_part} {val}")

        return "\n".join(lines) + "\n"


DEFAULT_REGISTRY = MetricsRegistry()
