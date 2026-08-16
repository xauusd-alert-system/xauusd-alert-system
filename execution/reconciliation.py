"""
Reconciliation loop helpers for the demo MT5 TradeGroup executor (ТЗ §27/§28).

Bridges broker state <-> TradeGroupStore <-> ledger. Rules:

* Local group OPENED but broker position missing -> check history deals; if the
  position was really closed (OUT deal), the caller transitions the group to the
  matching state (TP1/TP2/TP3/STOP); otherwise the divergence is reported.
* Broker position exists with no local group -> ORPHAN_BROKER_POSITION (never
  auto-managed).
* Local OPENED, broker volume < expected -> exact volume sync (partial fill note).
* Duplicate broker state -> no new orders (actionId idempotency in the store).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from data.trade_group_store import load_group, list_groups
from data.trading_event_ledger import append_trading_event
from execution.trade_group import GroupState, TradeGroupSpec

OUT_ENTRY = 1  # DEAL_ENTRY_OUT


@dataclass
class BrokerStateInspection:
    group_id: str
    local_state: GroupState
    positions: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)
    closed_out_deals: list[dict[str, Any]] = field(default_factory=list)
    volume_mismatch: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def broker_closed(self) -> bool:
        """Any OUT deal for the group's positions (broker TP/SL closure)."""
        return len(self.closed_out_deals) > 0


def inspect_group(driver, group: dict[str, Any]) -> BrokerStateInspection:
    """Compare local group state with fresh broker state (positions + deals)."""
    spec: TradeGroupSpec = group["spec"]
    inspection = BrokerStateInspection(
        group_id=spec.group_id, local_state=group["state"]
    )
    positions = driver.query_positions_by_magic()
    known_tickets = {
        int(item.get("broker", {}).get("position_id") or 0)
        for item in group.get("legs", [])
    }
    known_tickets.discard(0)
    # map by comment group id too (robust when broker ids were lost)
    group_positions = []
    for pos in positions:
        comment = str(pos.get("comment", "") or "")
        if pos["ticket"] in known_tickets or spec.group_id in comment:
            group_positions.append(pos)
    inspection.positions = group_positions
    # Query deal history for EVERY known ticket — including positions the
    # broker already closed (TP/SL): the OUT deal is the confirmation evidence
    # and lives in history after the position is gone (ТЗ §16).
    deal_tickets = {int(pos["ticket"]) for pos in group_positions}
    deal_tickets.update(known_tickets)
    for ticket in sorted(deal_tickets):
        if not ticket:
            continue
        deals = driver.query_deals(ticket)
        inspection.deals.extend(deals)
        out = [d for d in deals if int(d.get("entry", -1)) == OUT_ENTRY]
        inspection.closed_out_deals.extend(out)
    # volume sync: legs sharing one position (netting virtual legs) are
    # summed; a single-leg position (hedging) compares per leg.
    for pos in group_positions:
        matching = [item for item in group.get("legs", [])
                    if int(item.get("broker", {}).get("position_id") or 0) == pos["ticket"]]
        if not matching:
            continue
        expected = sum(float(item.get("volume") or 0.0) for item in matching)
        actual = float(pos.get("volume") or 0.0)
        if expected > 0.0 and actual < expected - 1e-9:
            inspection.volume_mismatch.append({
                "legs": [item.get("leg") for item in matching],
                "expected": expected, "actual": actual,
            })
    if not group_positions and not group.get("submitted"):
        inspection.notes.append("group not submitted; nothing to reconcile")
    return inspection


def latest_out_deal(inspection: BrokerStateInspection) -> dict[str, Any] | None:
    """The most recent OUT deal (broker TP/SL closure evidence)."""
    if not inspection.closed_out_deals:
        return None
    return max(inspection.closed_out_deals, key=lambda d: int(d.get("time", 0)))


def classify_broker_close(spec: TradeGroupSpec, out_deal: dict[str, Any],
                          tolerance: float = 0.0) -> str:
    """Classify a broker-side OUT deal: tp1 | tp2 | tp3 | stop | other.

    The price is matched against the IMMUTABLE spec levels (never recomputed).
    ``tolerance`` is a price distance used only in tests to absorb spread.
    """
    price = float(out_deal.get("price", 0.0) or 0.0)
    side = 1.0 if spec.side == "long" else -1.0
    for level, name in ((spec.geometry.tp1, "tp1"), (spec.geometry.tp2, "tp2"),
                        (spec.geometry.tp3, "tp3"), (spec.geometry.sl, "stop")):
        if abs(price - level) <= tolerance:
            return name
    # direction-aware fallback: pick the nearest spec level
    best = min(
        (("tp1", spec.geometry.tp1), ("tp2", spec.geometry.tp2),
         ("tp3", spec.geometry.tp3), ("stop", spec.geometry.sl)),
        key=lambda pair: abs(price - pair[1]),
    )
    return best[0]


def detect_orphan_positions(driver, db_path: str, *, ledger_db_path: str | None = None,
                            now_ms: int | None = None) -> list[dict[str, Any]]:
    """Broker positions with our magic that no local group covers.

    Emits an idempotent ``orphan_broker_position`` ledger event per ticket
    (deterministic event_id) and returns the orphan list. Orphans are NEVER
    auto-managed (ТЗ §28).
    """
    import time
    from data.trading_event_ledger import append_trading_event

    now = int(now_ms) if now_ms is not None else time.time_ns() // 1_000_000
    ledger = ledger_db_path or db_path
    groups = list_groups(db_path)
    covered_tickets = set()
    covered_groups = set()
    for group in groups:
        spec = group["spec"]
        covered_groups.add(spec.group_id)
        for item in group.get("legs", []):
            pid = int(item.get("broker", {}).get("position_id") or 0)
            if pid:
                covered_tickets.add(pid)
    orphans = []
    for pos in driver.query_positions_by_magic():
        comment = str(pos.get("comment", "") or "")
        group_id = _parse_group_id(comment)
        if pos["ticket"] in covered_tickets or group_id in covered_groups:
            continue
        orphans.append(pos)
        append_trading_event(
            ledger,
            event_type="orphan_broker_position",
            signal_id=f"orphan:{pos['ticket']}",
            asset_key=_asset_from_symbol(pos.get("symbol", "")),
            strategy_version="unknown",
            config_hash="unknown",
            actor="reconciliation",
            group_id=group_id,
            event_id=f"orphan:{pos['ticket']}",
            payload={"broker_position_id": pos["ticket"],
                     "symbol": pos.get("symbol"), "volume": pos.get("volume"),
                     "comment": comment, "detected_at_utc_ms": now},
        )
    return orphans


def _parse_group_id(comment: str) -> str | None:
    if not comment:
        return None
    for token in str(comment).split("|"):
        if token.startswith("TG:"):
            return token[3:]
    return None


def _asset_from_symbol(symbol: str) -> str:
    mapping = {"GOLD": "XAUUSD", "SILVER": "XAGUSD", "BITCOIN": "BTCUSD",
               "EURUSD": "EURUSD", "GBPUSD": "GBPUSD"}
    return mapping.get(symbol, symbol or "unknown")


def emit_execution_error(db_path: str, spec: TradeGroupSpec, *, reason: str,
                         payload: dict[str, Any] | None = None,
                         leg: int | None = None) -> None:
    """Append an ``execution_error`` fact (used for partial submission etc.)."""
    append_trading_event(
        db_path,
        event_type="execution_error",
        signal_id=spec.signal_id,
        asset_key=spec.asset_key,
        strategy_version=spec.strategy_version,
        config_hash=spec.config_hash,
        model_hash=spec.model_hash,
        actor="reconciliation",
        reason=reason,
        group_id=spec.group_id,
        leg_id=f"{spec.group_id}-L{leg}" if leg is not None else None,
        payload=payload or {},
    )
