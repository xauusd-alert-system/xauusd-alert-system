"""Free-disk-space check (ТЗ 6.18 / P2-31).

Pure stdlib (no psutil / os.statvfs — the latter does not exist on Windows);
uses ``shutil.disk_usage``. Returns a structured result so both the alert
rules (monitoring/alerts.py) and the CLI can consume it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class DiskStatus:
    path: str
    free_mb: float
    total_mb: float

    @property
    def pct_used(self) -> float:
        if self.total_mb <= 0:
            return 0.0
        return round(100.0 * (1.0 - self.free_mb / self.total_mb), 2)


def disk_status(path: str = ".") -> DiskStatus:
    usage = shutil.disk_usage(path)
    return DiskStatus(
        path=path,
        free_mb=round(usage.free / (1024 * 1024), 2),
        total_mb=round(usage.total / (1024 * 1024), 2),
    )


def check_disk_space(path: str = ".", min_free_mb: float = 500.0) -> bool:
    """True when free space is >= min_free_mb (ТЗ 6.18 semantics)."""
    return disk_status(path).free_mb >= float(min_free_mb)
