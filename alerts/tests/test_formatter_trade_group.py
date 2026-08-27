"""Telegram formatter tests for the TradeGroupSpec v1 authoritative path (ТЗ §19–§22)."""
from __future__ import annotations

import pytest

from alerts.formatter import (
    format_clean_signal_message,
    format_group_lifecycle_update,
    format_signal_message,
    format_trade_group_message,
    geometry_from_spec,
)
from execution.trade_group import TradeGroupSpec

SPEC_DICT = {
    "schema_version": "trade-group.v1",
    "group_id": "TG-20260816-000042",
    "signal_id": "SGL-20260816-000042",
    "intent_id": "INT-20260816-000042",
    "asset_key": "XAUUSD",
    "broker_symbol": "GOLD",
    "mode": "paper",
    "side": "long",
    "entry": {"low": 4159.10, "high": 4159.50, "reference": 4159.30},
    "geometry": {"version": "xau_m15_intraday_v1", "unit": "price",
                 "step_price": 4.30, "tp1": 4163.60, "tp2": 4167.70,
                 "tp3": 4171.20, "sl": 4140.30},
    "targets": [
        {"leg": 1, "price": 4163.60, "allocation": 0.333333},
        {"leg": 2, "price": 4167.70, "allocation": 0.333333},
        {"leg": 3, "price": 4171.20, "allocation": 0.333334},
    ],
    "break_even": {"trigger": "tp1_filled",
                   "raw_price_policy": "actual_fill",
                   "protected_price_policy": "actual_fill_plus_cost_buffer",
                   "apply_to": [2, 3]},
    "risk": {"currency": "USD", "max_cash": 25.0, "max_pct": 0.50,
             "estimated_loss_at_sl": 24.73, "total_volume": 0.03},
    "profile_id": "xau_m15_intraday_v1",
    "model_version": "v3", "model_hash": "m" * 64, "config_hash": "c" * 64,
    "strategy_version": "s3",
    "expires_at_utc_ms": 1_760_000_000_000,   # 2025-10-08-ish; only HH:MM shown
    "created_at_utc_ms": 1_700_000_000_000,
}


def test_format_trade_group_message_layout():
    spec = TradeGroupSpec.model_validate(SPEC_DICT)
    msg = format_trade_group_message(spec)
    assert "🟢 ЛОНГ · XAUUSD" in msg
    assert "Режим: PAPER" in msg
    assert "Group: TG-20260816-000042" in msg
    assert "Зона входа: 4159.1 — 4159.5" in msg
    assert "Стоп: 4140.3" in msg
    assert "TP1: 4163.6 · 33.33%" in msg
    assert "TP2: 4167.7 · 33.33%" in msg
    assert "TP3: 4171.2 · 33.34%" in msg
    assert "SL остатка → BE + cost buffer" in msg
    assert "Profile: xau_m15_intraday_v1" in msg
    assert "UTC" in msg


def test_short_spec_uses_red_emoji():
    short = dict(SPEC_DICT)
    short["side"] = "short"
    short["entry"] = {"low": 4159.10, "high": 4159.50, "reference": 4159.30}
    short["geometry"] = {"version": "xau_m15_intraday_v1", "unit": "price",
                         "step_price": 4.30, "tp1": 4155.00, "tp2": 4150.70,
                         "tp3": 4146.40, "sl": 4179.30}
    short["targets"] = [
        {"leg": 1, "price": 4155.00, "allocation": 0.333333},
        {"leg": 2, "price": 4150.70, "allocation": 0.333333},
        {"leg": 3, "price": 4146.40, "allocation": 0.333334},
    ]
    msg = format_trade_group_message(TradeGroupSpec.model_validate(short))
    assert "🔴 ШОРТ · XAUUSD" in msg
    assert "Стоп: 4179.3" in msg


def test_no_recomputation_for_trade_group_v1():
    """ТЗ §19/§28.3: for trade-group.v1 the formatter must render the FINAL
    geometry even when the payload also carries conflicting legacy fields."""
    conflicting = dict(SPEC_DICT)
    conflicting.update({
        "step": 99.0,                      # would change levels if recomputed
        "atr": 50.0,
        "entry_zone": [100.0, 101.0],      # would change entry if recomputed
        "invalidation": 1000.0,            # would change SL if recomputed
        "confidence": 0.99,
    })
    msg = format_clean_signal_message(conflicting, asset_key="XAUUSD")
    assert "TP1: 4163.6" in msg
    assert "TP2: 4167.7" in msg
    assert "TP3: 4171.2" in msg
    assert "Стоп: 4140.3" in msg
    # recomputed levels (step 99 / invalidation 1000) must NOT appear anywhere
    assert "4258" not in msg and "99" not in msg and "1000" not in msg


def test_missing_final_geometry_is_formatter_error():
    broken = dict(SPEC_DICT)
    geometry = dict(broken["geometry"])
    geometry["tp2"] = None
    broken["geometry"] = geometry
    with pytest.raises(ValueError, match="formatter_error"):
        format_clean_signal_message(broken)


def test_group_spec_embedded_dict_route():
    wrapped = {"bias": "long", "group_spec": SPEC_DICT}
    msg = format_clean_signal_message(wrapped, asset_key="XAUUSD")
    assert "Group: TG-20260816-000042" in msg


def test_legacy_fallback_still_works():
    """Legacy equal-step fallback (only for signals WITHOUT trade-group.v1)."""
    legacy = {
        "bias": "long", "confidence": 0.7, "regime": "trend_up",
        "session": "london", "entry_zone": [4159.10, 4159.50],
        "step": 4.30, "invalidation": 4140.30,
    }
    msg = format_signal_message(legacy, asset_key="XAUUSD")
    assert "TP1: 4163.6" in msg     # 4159.3 + 4.3
    assert "TP2: 4167.9" in msg     # 4159.3 + 2*4.3
    # legacy equal-step fallback uses 3*step stop when no target legs supplied
    assert "Стоп: 4146.4" in msg    # 4159.3 - 3*4.3


def test_legacy_path_never_extends_supplied_legs():
    """ТЗ §28.3 regression: supplied target legs are authoritative in legacy too."""
    signal = {
        "bias": "short", "entry_zone": [4200.0, 4201.0],
        "target_legs": [{"price": 4196.0}, {"price": 4192.0}, {"price": 4188.0}],
        "invalidation": 4212.0,
    }
    msg = format_signal_message(signal, asset_key="XAUUSD")
    assert "TP1: 4196" in msg and "TP2: 4192" in msg and "TP3: 4188" in msg
    assert "Стоп: 4212" in msg


def test_lifecycle_update_format():
    msg = format_group_lifecycle_update(
        group_id="TG-20260816-000042",
        event_type="✅ TP1 FILLED",
        state="BE_REQUESTED",
        remaining_legs=2,
        sl_price=4160.03,
        timestamp_utc="2026-08-16T12:45:00Z",
    )
    assert "Group: TG-20260816-000042" in msg
    assert "✅ TP1 FILLED" in msg
    assert "Remaining legs: 2" in msg
    assert "SL remaining legs: 4160.03" in msg
    assert "State: BE_REQUESTED" in msg
    assert "2026-08-16T12:45:00Z" in msg


def test_geometry_from_spec_is_authoritative():
    spec = TradeGroupSpec.model_validate(SPEC_DICT)
    geometry = geometry_from_spec(spec)
    assert geometry["tp1"] == 4163.60
    assert geometry["sl"] == 4140.30
    assert geometry["schema_version"] == "trade-group.v1"
    assert geometry["group_id"] == "TG-20260816-000042"


# ==========================================================================
# Follow-up ТЗ §13: full SHORT parity — no mirroring, no rebuild, order kept
# ==========================================================================

SHORT_100 = {
    "schema_version": "trade-group.v1",
    "group_id": "TG-SHORT-1",
    "signal_id": "SGL-SHORT-1",
    "intent_id": "INT-SHORT-1",
    "asset_key": "XAUUSD",
    "broker_symbol": "GOLD",
    "mode": "paper",
    "side": "short",
    "entry": {"low": 99.0, "high": 101.0, "reference": 100.0},
    "geometry": {"version": "dir_v1", "unit": "price", "step_price": 4.0,
                 "tp1": 96.0, "tp2": 92.0, "tp3": 88.0, "sl": 110.0},
    "targets": [
        {"leg": 1, "price": 96.0, "allocation": 0.333333},
        {"leg": 2, "price": 92.0, "allocation": 0.333333},
        {"leg": 3, "price": 88.0, "allocation": 0.333334},
    ],
    "break_even": {"trigger": "tp1_filled",
                   "raw_price_policy": "actual_fill",
                   "protected_price_policy": "actual_fill_plus_cost_buffer",
                   "apply_to": [2, 3]},
    "risk": {"currency": "USD", "max_cash": 25.0, "max_pct": 0.5,
             "estimated_loss_at_sl": 24.0, "total_volume": 0.03},
    "profile_id": "dir_v1",
    "model_version": "v3", "model_hash": "m" * 64, "config_hash": "c" * 64,
    "strategy_version": "s3",
    "expires_at_utc_ms": 1_800_000_000_000, "created_at_utc_ms": 1_700_000_000_000,
}


def test_short_parity_full_layout():
    """Follow-up ТЗ §13: entry 100 / TP1 96 / TP2 92 / TP3 88 / SL 110."""
    msg = format_trade_group_message(TradeGroupSpec.model_validate(SHORT_100))
    assert "🔴 ШОРТ · XAUUSD" in msg
    assert "Зона входа: 99 — 101" in msg
    assert "TP1: 96 · 33.33%" in msg
    assert "TP2: 92 · 33.33%" in msg
    assert "TP3: 88 · 33.34%" in msg
    assert "Стоп: 110" in msg
    # no mirroring / no rebuild / no legacy step / order preserved
    assert msg.index("TP1") < msg.index("TP2") < msg.index("TP3")
    assert "104" not in msg and "108" not in msg and "112" not in msg
    assert "90" not in msg
    # parity with the single authoritative payload
    spec = TradeGroupSpec.model_validate(SHORT_100)
    assert geometry_from_spec(spec) == spec.as_geometry_payload()


# ==========================================================================
# Follow-up ТЗ §14: every missing final-geometry field -> formatter_error
# ==========================================================================

@pytest.mark.parametrize("field", ["tp1", "tp2", "tp3", "sl", "entry.reference"])
def test_missing_geometry_field_is_formatter_error(field):
    broken = dict(SHORT_100)
    if field.startswith("entry"):
        entry = dict(broken["entry"])
        entry["reference"] = None
        broken["entry"] = entry
    else:
        geometry = dict(broken["geometry"])
        geometry[field] = None
        broken["geometry"] = geometry
    with pytest.raises(ValueError, match="formatter_error"):
        format_clean_signal_message(broken)
    # and never a fallback: no ATR/step recomputation path is reachable


# P2-21 / TZ Часть 7 п.7.2: single formatting path through the trade group
# builder. The v1 path is PRIMARY (no deprecation); the legacy recomputation
# path is deprecated and must warn.

def test_formatter_uses_geometry_payload():
    """P2-21: the trade-group.v1 path reads levels straight from
    spec.as_geometry_payload() — it is the primary path, must NOT emit a
    deprecation warning, and its levels must equal the authoritative payload."""
    import warnings as _warnings

    spec = TradeGroupSpec.model_validate(SPEC_DICT)
    with _warnings.catch_warnings():
        _warnings.simplefilter("error", DeprecationWarning)
        msg = format_clean_signal_message(SPEC_DICT)  # v1 dict goes to v1 path

    payload = spec.as_geometry_payload()
    from alerts.formatter import _fmt_price

    assert f"TP1: {_fmt_price(payload['tp1'])}" in msg
    assert f"Стоп: {_fmt_price(payload['sl'])}" in msg
    assert f"Group: {SPEC_DICT['group_id']}" in msg


def test_legacy_path_warns():
    """P2-21: a legacy signal (no group_spec / schema_version) goes through
    the deprecated recomputation path — it still formats (backwards compat
    for in-flight legacy signals) but MUST raise a DeprecationWarning."""
    legacy = {
        "bias": "long",
        "entry": 4159.3,
        "step": 4.3,
        "targets": [4163.6, 4167.9, 4172.2],
        "invalidation": 4146.4,
    }
    with pytest.warns(DeprecationWarning, match="legacy signal formatting"):
        msg = format_signal_message(legacy, asset_key="XAUUSD")
    assert "TP1: 4163.6" in msg      # legacy rendering still works
    assert "Group:" not in msg       # ...but it is NOT the trade-group path
