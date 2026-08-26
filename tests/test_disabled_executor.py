# -*- coding: utf-8 -*-
"""Stage B: DisabledExecutor — the only executor allowed in signal-only
profiles. Any submit attempt must be logged AND raise."""
import logging

import pytest

from execution.disabled_executor import (
    DisabledExecutor,
    ExecutionDisabledError,
    OrderRequest,
    executor_for_profile,
)


def _order() -> OrderRequest:
    return OrderRequest(
        symbol="AMD", side="buy", qty=20, order_type="market",
        price=None, ref="sig_test_001",
        meta={"stop": 200.70, "tp1": 201.70, "tp2": 202.20},
    )


def test_submit_raises_and_logs_full_payload(caplog):
    ex = DisabledExecutor(reason="profile=us_stocks_challenge")
    with caplog.at_level(logging.WARNING, logger="execution.disabled"):
        with pytest.raises(ExecutionDisabledError):
            ex.submit(_order())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "ORDER BLOCKED" in text
    assert "AMD" in text and "buy" in text and "sig_test_001" in text


def test_error_message_names_signal_only_policy():
    ex = DisabledExecutor()
    with pytest.raises(ExecutionDisabledError, match="signal-only"):
        ex.submit(_order())


def test_executor_for_profile_returns_disabled_executor():
    assert isinstance(executor_for_profile("us_stocks_challenge"), DisabledExecutor)
    assert isinstance(executor_for_profile("replay"), DisabledExecutor)


def test_executor_for_profile_rejects_legacy_profiles():
    for prof in ("forex_legacy", "crypto_legacy"):
        with pytest.raises(ValueError, match="signal-only profiles"):
            executor_for_profile(prof)


def test_disabled_executor_satisfies_executor_protocol():
    # Structural conformance with the Executor Protocol from ТЗ §5.
    ex: DisabledExecutor = DisabledExecutor()
    assert callable(getattr(ex, "submit"))


def test_order_request_roundtrip_dict():
    d = _order().to_dict()
    assert d["symbol"] == "AMD" and d["meta"]["tp2"] == 202.20
