"""P2-4 smoke tests: execution.telegram_formatter pure formatting functions.

These are light smoke checks (not None, key elements present) — the full
end-to-end notification behavior is covered by the executor tests that exercise
the thin ``_*_message`` delegates in ``mt5_trade_group``.
"""
from __future__ import annotations

import pytest

from execution import telegram_formatter as tf
from execution.tests.test_mt5_trade_group import make_spec


@pytest.fixture()
def spec():
    return make_spec()


def test_format_group_opened(spec):
    text = tf.format_group_opened(spec)
    assert text is not None
    assert "TRADE GROUP OPENED" in text
    assert spec.group_id in text
    assert "TP1:" in text and "TP3:" in text and "SL:" in text
    assert "Mode: DEMO" in text


def test_format_tp1_filled(spec):
    text = tf.format_tp1_filled(spec)
    assert text is not None
    assert "TP1 FILLED" in text
    assert spec.group_id in text
    assert f"Leg {spec.break_even.apply_to[0]}" in text
    assert f"Leg {spec.break_even.apply_to[1]}" in text


def test_format_tp_filled(spec):
    text = tf.format_tp_filled(spec, label="tp2", header="✅ TP2 FILLED")
    assert text is not None
    assert "TP2 FILLED" in text
    assert spec.group_id in text


def test_format_be_confirmed(spec):
    text = tf.format_be_confirmed(spec, 1234.5)
    assert text is not None
    assert "BE CONFIRMED" in text
    assert "1234.5" in text


def test_format_stopped(spec):
    text = tf.format_stopped(spec)
    assert text is not None
    assert "STOPPED" in text
    assert spec.group_id in text


def test_format_partial_submission(spec):
    text = tf.format_partial_submission(spec, [1, 2], [3])
    assert text is not None
    assert "PARTIAL SUBMISSION" in text
    assert "Opened legs: 1, 2" in text
    assert "Rejected: leg 3" in text
    assert "Compensation: IN PROGRESS" in text


def test_format_partial_submission_no_rejected(spec):
    text = tf.format_partial_submission(spec, [1, 2, 3], [])
    assert "Rejected:" in text
    assert "Opened legs: 1, 2, 3" in text


def test_format_failed_after_compensation(spec):
    text = tf.format_failed_after_compensation(spec, "broker reject")
    assert text is not None
    assert "TRADE GROUP FAILED" in text
    assert "Reason: broker reject" in text
    assert "Open risk: 0" in text


def test_format_open_risk(spec):
    text = tf.format_open_risk(spec, ["pos-1", "pos-2"])
    assert text is not None
    assert "EXECUTION ERROR" in text
    assert "FAILED_WITH_OPEN_RISK" in text
    assert "pos-1, pos-2" in text
