"""TZ_BOOKS: the `books:` config section and its loader contract."""
from __future__ import annotations

import pytest

from config.loader import BOOKS_DEFAULTS, books_config


def test_defaults_resolve_from_empty_config():
    bc = books_config({})
    assert bc["trade_level"] == 0.6
    assert bc["samples"]["split"] == [0.6, 0.2, 0.2]
    assert bc["bridge"]["schema_version"] == 1
    assert bc["drift"]["psi_alarm"] == 0.25


def test_config_file_section_is_valid():
    from config.loader import load_config
    bc = books_config(load_config("config/config.yaml"))
    assert bc["samples"]["asset"] == "XAUUSD"
    assert bc["validation"]["tick_mode"] == "real_ticks"
    assert bc["tester_criterion"]["dd_penalty_weight"] == 1.0


def test_partial_override_keeps_other_defaults():
    bc = books_config({"books": {"trade_level": 0.7,
                                 "samples": {"window": 8}}})
    assert bc["trade_level"] == 0.7
    assert bc["samples"]["window"] == 8
    assert bc["samples"]["horizon"] == BOOKS_DEFAULTS["samples"]["horizon"]


def test_result_is_a_copy():
    original = {"books": {"trade_level": 0.6}}
    bc = books_config(original)
    bc["trade_level"] = 0.99
    assert original["books"]["trade_level"] == 0.6


def test_unknown_keys_are_rejected():
    with pytest.raises(KeyError):
        books_config({"books": {"trade_leve1": 1}})
    with pytest.raises(ValueError):
        books_config({"books": "not-a-mapping"})
