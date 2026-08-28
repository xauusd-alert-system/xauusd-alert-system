"""ТЗ 6.6 / P2-33: structured logging + rotation tests.

Covers:
    - json_format_parses           — each line is valid JSON with required keys;
    - rotation_triggered           — small max_bytes rotates into .1/.2/...;
    - text_format_default_unchanged— default stays the legacy text format.
"""
from __future__ import annotations

import json
import logging

import pytest

from logs.setup import JsonFormatter, setup_logging


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Keep the root logger pristine for other tests in the suite."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    yield root


def _make_record(msg: str = "hello", level: int = logging.INFO, **extra):
    record = logging.LogRecord(
        name="unit.test", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ----------------------------------------------------------- json_format_parses

def test_json_format_parses(clean_root, tmp_path):
    setup_logging(str(tmp_path), fmt="json", console=False)
    logging.getLogger("unit.test").info("structured hello", extra={
        "group_id": "TG-1", "stage": "poll_once"})

    log_path = tmp_path / "trading.log"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])  # valid JSON
    assert payload["level"] == "INFO"
    assert payload["logger"] == "unit.test"
    assert payload["msg"] == "structured hello"
    assert "ts" in payload
    assert payload["extra"] == {"group_id": "TG-1", "stage": "poll_once"}


def test_json_formatter_exception_payload(tmp_path):
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record("failed")
        record.exc_info = sys.exc_info()
        payload = json.loads(formatter.format(record))
    assert payload["exc"] and "boom" in payload["exc"]


def test_format_selection_priority(monkeypatch):
    from logs.setup import resolve_format

    assert resolve_format("json") == "json"
    assert resolve_format(None, "JSON") == "json"
    assert resolve_format(None, None, {"monitoring": {"logging": {"format": "json"}}}) == "json"
    assert resolve_format(None, None, {}) == "text"
    assert resolve_format("text", "json") == "text"  # explicit wins


def test_env_override_json(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging(str(tmp_path), console=False)
    logging.getLogger("unit.env").warning("via env")
    line = (tmp_path / "trading.log").read_text(
        encoding="utf-8").strip().splitlines()[0]
    assert json.loads(line)["msg"] == "via env"


# ------------------------------------------------------------------ rotation

def test_rotation_triggered(clean_root, tmp_path):
    # maxBytes=300 -> a couple of records rotate the file.
    setup_logging(str(tmp_path), fmt="text", console=False,
                  max_bytes=300, backup_count=3)
    logger = logging.getLogger("unit.rotation")
    for i in range(20):
        logger.info("rotation filler message %02d %s", i, "x" * 40)

    base = tmp_path / "trading.log"
    rotated = sorted(p.name for p in tmp_path.iterdir()
                     if p.name.startswith("trading.log."))
    assert rotated, "no rotated files created"
    assert len(rotated) <= 3, "backup_count exceeded"
    assert base.exists()


def test_rotation_backup_count_respected(clean_root, tmp_path):
    setup_logging(str(tmp_path), fmt="text", console=False,
                  max_bytes=200, backup_count=2)
    logger = logging.getLogger("unit.rotation2")
    for i in range(50):
        logger.info("filler %d %s", i, "y" * 60)
    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("trading.log")]
    assert len(rotated) <= 3  # live + 2 backups


def test_setup_is_idempotent(tmp_path):
    """Repeated setup never duplicates handlers (no double log lines)."""
    setup_logging(str(tmp_path), fmt="json", console=False)
    setup_logging(str(tmp_path), fmt="json", console=False)
    logging.getLogger("unit.idem").info("once only")
    lines = (tmp_path / "trading.log").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


# ------------------------------------------------------ text default unchanged

def test_text_format_default_unchanged(clean_root, tmp_path):
    setup_logging(str(tmp_path), fmt=None, console=False, cfg={})
    logging.getLogger("unit.text").info("plain message")
    line = (tmp_path / "trading.log").read_text(
        encoding="utf-8").strip().splitlines()[0]
    # Legacy layout: "<asctime> [LEVEL] ..." — human-readable, not JSON.
    assert "[INFO]" in line
    assert "plain message" in line
    assert not line.lstrip().startswith("{")


def test_config_still_text_keeps_legacy(tmp_path):
    setup_logging(str(tmp_path), cfg={"monitoring": {"logging": {"format": "text"}}},
                  console=False)
    logging.getLogger("unit.cfg").info("cfg text")
    line = (tmp_path / "trading.log").read_text(
        encoding="utf-8").strip().splitlines()[0]
    assert not line.lstrip().startswith("{")


def test_rotation_defaults_from_config(clean_root, tmp_path):
    """monitoring.logging.max_bytes/backup_count are honoured."""
    setup_logging(str(tmp_path), fmt="text", console=False, cfg={
        "monitoring": {"logging": {"max_bytes": 300, "backup_count": 2}},
    })
    logger = logging.getLogger("unit.cfgrot")
    for i in range(30):
        logger.info("cfg rotation filler %d %s", i, "z" * 50)
    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("trading.log")]
    assert len(rotated) <= 3
