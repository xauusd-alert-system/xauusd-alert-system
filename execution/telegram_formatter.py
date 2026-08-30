"""
Telegram message formatting for MT5 trade-group notifications (ТЗ §35/§36).

P2-4: pure extraction of the ``_*_message`` helpers from
``execution.mt5_trade_group`` — these functions produce ONLY broker-confirmed
event texts and depend on nothing but the immutable ``TradeGroupSpec`` (plus
explicit data arguments). No I/O, no broker access, trivially testable.
"""

from __future__ import annotations

from typing import Any

from execution.trade_group import TradeGroupSpec

__all__ = [
    "format_group_opened",
    "format_tp1_filled",
    "format_tp_filled",
    "format_be_confirmed",
    "format_stopped",
    "format_partial_submission",
    "format_failed_after_compensation",
    "format_open_risk",
]


def format_group_opened(spec: TradeGroupSpec) -> str:
    return (
        f"🔥 TRADE GROUP OPENED\n{spec.asset_key}\n"
        f"{'LONG' if spec.side == 'long' else 'SHORT'}\n\n"
        f"Group: {spec.group_id}\n"
        f"Entry: {spec.entry.actual_fill or spec.entry.reference}\n"
        f"TP1: {spec.geometry.tp1}\nTP2: {spec.geometry.tp2}\n"
        f"TP3: {spec.geometry.tp3}\nSL: {spec.geometry.sl}\n"
        f"Mode: DEMO"
    )


def format_tp1_filled(spec: TradeGroupSpec) -> str:
    return (
        f"✅ TP1 FILLED\nGroup: {spec.group_id}\n\n"
        f"Leg 1: CLOSED\n\nBE requested for:\n"
        f"Leg {spec.break_even.apply_to[0]}\nLeg {spec.break_even.apply_to[1]}\n"
        f"Mode: DEMO"
    )


def format_tp_filled(spec: TradeGroupSpec, label: str, header: str) -> str:
    return f"{header}\nGroup: {spec.group_id}\nMode: DEMO"


def format_be_confirmed(spec: TradeGroupSpec, sl_price: float) -> str:
    return f"🟢 BE CONFIRMED\nGroup: {spec.group_id}\n\nSL remaining legs: {sl_price}\nMode: DEMO"


def format_stopped(spec: TradeGroupSpec) -> str:
    return f"🛑 STOPPED\nGroup: {spec.group_id}\nMode: DEMO"


def format_partial_submission(spec: TradeGroupSpec, opened_legs: list[int], rejected_legs: list[int]) -> str:
    return (
        f"⚠️ TRADE GROUP PARTIAL SUBMISSION\nGroup: {spec.group_id}\n"
        f"Opened legs: {', '.join(str(l) for l in opened_legs) or '-'}\n"
        f"Rejected: {', '.join('leg ' + str(l) for l in rejected_legs)}\n"
        f"Compensation: IN PROGRESS\nMode: DEMO"
    )


def format_failed_after_compensation(spec: TradeGroupSpec, reason: str) -> str:
    return (
        f"🛑 TRADE GROUP FAILED\nGroup: {spec.group_id}\n"
        f"Reason: {reason}\nCompensation: CONFIRMED\nOpen risk: 0\nMode: DEMO"
    )


def format_open_risk(spec: TradeGroupSpec, open_refs: list[Any]) -> str:
    return (
        f"🚨 EXECUTION ERROR\nGroup: {spec.group_id}\n"
        f"State: FAILED_WITH_OPEN_RISK\n"
        f"Open legs: {', '.join(str(r) for r in open_refs)}\n"
        f"Compensation: FAILED\nMode: DEMO"
    )
