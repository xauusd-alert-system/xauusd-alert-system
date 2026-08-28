"""Auditable setup lifecycle transitions backed by the primary event ledger."""
from __future__ import annotations

import json

from data.trading_event_ledger import append_trading_event, read_trading_events

TRANSITIONS = {
    None: {"watch", "no_trade"},
    "watch": {"armed", "rejected", "expired"},
    "armed": {"confirmed", "rejected", "expired"},
    "confirmed": set(), "rejected": set(), "expired": set(), "no_trade": set(),
}
EVENT_FOR_STATE = {
    "watch": "signal_created", "armed": "signal_armed", "confirmed": "signal_confirmed",
    "rejected": "signal_rejected", "expired": "signal_expired", "no_trade": "signal_created",
}


def latest_signal_state(db_path: str, signal_id: str) -> str | None:
    events = read_trading_events(db_path, signal_id)
    if events.empty:
        return None
    for row in reversed(events.to_dict("records")):
        payload = json.loads(row["payload_json"])
        if payload.get("state"):
            return payload["state"]
    return None


def transition_signal(db_path: str, *, signal_id: str, new_state: str, asset_key: str,
                      strategy_version: str, config_hash: str, actor: str,
                      reason: str, payload: dict | None = None, **hashes) -> str:
    current = latest_signal_state(db_path, signal_id)
    if new_state not in TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid signal transition {current!r} -> {new_state!r}")
    body = dict(payload or {})
    body["state"] = new_state
    return append_trading_event(
        db_path, event_type=EVENT_FOR_STATE[new_state], signal_id=signal_id,
        asset_key=asset_key, strategy_version=strategy_version, config_hash=config_hash,
        actor=actor, reason=reason, payload=body, **hashes,
    )
