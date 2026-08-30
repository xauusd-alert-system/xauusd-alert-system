"""ТЗ 6.6 / P2-33: structured logging setup with rotation.

``setup_logging`` configures the root logger with:

    * format: ``text`` (default — the legacy human format, fully backward
      compatible) or ``json`` (one JSON object per line: ts, level, logger,
      message, extra);
    * a ``RotatingFileHandler`` for ``logs/trading.log`` (P2-33) with
      configurable ``max_bytes`` (default 10 MB) and ``backup_count``
      (default 5);
    * an optional console StreamHandler.

Selection order: explicit argument > env ``LOG_FORMAT`` > config
``monitoring.logging.format`` > "text". Idempotent: calling it twice replaces
the handlers it installed instead of duplicating them.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

LOGGER_NAME_ROOT = None  # root

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 5

_INSTALLED_ATTR = "_xauusd_monitoring_handlers"


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts, level, logger, message, extra."""

    def __init__(self, *, extra_keys: list[str] | None = None):
        super().__init__()
        self._extra_keys = extra_keys or []

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key
            not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "taskName",
                "message",
                "asctime",
            }
            and not key.startswith("_")
        }
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Legacy human format — the historical default is preserved exactly."""

    def __init__(self):
        super().__init__(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def resolve_format(
    explicit: str | None = None,
    env_value: str | None = None,
    cfg: dict | None = None,
) -> str:
    """explicit arg > LOG_FORMAT env > config monitoring.logging.format > text."""
    if explicit in ("json", "text"):
        return explicit
    candidate = (env_value or "").strip().lower()
    if candidate in ("json", "text"):
        return candidate
    cfg_fmt = (((cfg or {}).get("monitoring") or {}).get("logging") or {}).get("format")
    if cfg_fmt in ("json", "text"):
        return str(cfg_fmt)
    return "text"


def resolve_rotation(
    cfg: dict | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> tuple[int, int]:
    """(max_bytes, backup_count) — config monitoring.logging.* with defaults."""
    cfg_log = ((cfg or {}).get("monitoring") or {}).get("logging") or {}
    resolved_max = int(max_bytes if max_bytes is not None else cfg_log.get("max_bytes", _DEFAULT_MAX_BYTES))
    resolved_backups = int(
        backup_count if backup_count is not None else cfg_log.get("backup_count", _DEFAULT_BACKUP_COUNT)
    )
    return max(1, resolved_max), max(0, resolved_backups)


def setup_logging(
    log_dir: str = "logs",
    *,
    fmt: str | None = None,
    level: str = "INFO",
    cfg: dict | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    log_file: str = "trading.log",
    console: bool = True,
) -> logging.Handler:
    """Configure the root logger; returns the installed file handler.

    Idempotent: previously installed monitoring handlers are removed before
    new ones are attached, so repeated calls (tests, re-config) never
    duplicate output lines.
    """
    os.makedirs(log_dir, exist_ok=True)
    resolved = resolve_format(explicit=fmt, env_value=os.getenv("LOG_FORMAT"), cfg=cfg)
    r_max, r_backups = resolve_rotation(cfg, max_bytes, backup_count)

    formatter = JsonFormatter() if resolved == "json" else TextFormatter()
    handler: RotatingFileHandler = RotatingFileHandler(
        os.path.join(log_dir, log_file),
        maxBytes=r_max,
        backupCount=r_backups,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger(LOGGER_NAME_ROOT)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # Idempotency: drop handlers installed by a previous setup_logging call.
    for stale in list(getattr(root, _INSTALLED_ATTR, [])):
        try:
            root.removeHandler(stale)
            stale.close()
        except Exception:  # noqa: BLE001 — cleanup must never raise
            pass
    installed: list[logging.Handler] = [handler]

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
        installed.append(stream)

    root.addHandler(handler)
    setattr(root, _INSTALLED_ATTR, installed)
    return handler
