"""Append-only broker execution-attempt ledger for empirical cost modelling.

Unlike ``executed_trades`` (one mutable row per completed trade), this table keeps
EVERY order attempt: fills, partial fills and rejections.  Requested/fill prices
and timestamps make spread/slippage/latency distributions reproducible per asset.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pandas as pd

from data.storage import get_connection, read_candles

TABLE_NAME = "execution_fills"

# Wave-0 MQL5 plan: correlate execution attempts with the SignalIntent that
# created them and tag the fact's precision (request/probe/passive/...).
# Columns are nullable and added in-place so legacy databases migrate
# non-destructively (same pattern as data/storage.py OPTIONAL_MARKET_COLUMNS).
OPTIONAL_EXECUTION_COLUMNS = ["intent_id", "precision"]


def init_execution_ledger(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_key TEXT NOT NULL,
                broker_symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_at_ms INTEGER NOT NULL,
                completed_at_ms INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                requested_price REAL,
                filled_price REAL,
                adverse_slippage REAL,
                volume_requested REAL,
                volume_filled REAL,
                status TEXT NOT NULL,
                retcode INTEGER,
                rejection_reason TEXT,
                order_ticket INTEGER,
                position_ticket INTEGER,
                intent_id TEXT,
                precision TEXT,
                metadata_json TEXT NOT NULL
            );
        """)
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME});")}
        for column in OPTIONAL_EXECUTION_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} TEXT")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_asset_time "
            f"ON {TABLE_NAME}(asset_key, requested_at_ms);"
        )
        conn.commit()
    finally:
        conn.close()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def log_execution_attempt(
    db_path: str,
    *,
    asset_key: str,
    broker_symbol: str,
    action: str,
    side: str,
    requested_at_ms: int,
    completed_at_ms: int | None = None,
    requested_price: float | None = None,
    filled_price: float | None = None,
    volume_requested: float | None = None,
    volume_filled: float | None = None,
    status: str,
    retcode: int | None = None,
    rejection_reason: str | None = None,
    order_ticket: int | None = None,
    position_ticket: int | None = None,
    intent_id: str | None = None,
    precision: str | None = "request",
    metadata: dict[str, Any] | None = None,
) -> int:
    """Append one immutable attempt and return its ledger id.

    ``adverse_slippage`` is positive when execution moved against the request:
    fill-request for buys, request-fill for sells. It remains NULL for rejects.
    ``intent_id`` links the attempt to its SignalIntent; ``precision`` tags the
    fact's nature (``request`` from the sender, ``probe`` from the FX probe,
    etc.) so cost statistics never mix observation modes.
    """
    completed = int(completed_at_ms if completed_at_ms is not None else now_ms())
    requested = int(requested_at_ms)
    normalized_side = str(side).lower()
    if normalized_side not in {"buy", "sell", "none"}:
        raise ValueError("side must be buy, sell or none")
    if status not in {"filled", "partial", "rejected", "dry_run"}:
        raise ValueError("unsupported execution status")

    slippage = None
    if requested_price is not None and filled_price is not None:
        direction = 1.0 if normalized_side == "buy" else -1.0
        slippage = direction * (float(filled_price) - float(requested_price))

    init_execution_ledger(db_path)
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            f"""INSERT INTO {TABLE_NAME} (
                asset_key, broker_symbol, action, side, requested_at_ms,
                completed_at_ms, latency_ms, requested_price, filled_price,
                adverse_slippage, volume_requested, volume_filled, status,
                retcode, rejection_reason, order_ticket, position_ticket,
                intent_id, precision, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                asset_key, broker_symbol, action, normalized_side, requested,
                completed, max(0, completed - requested), requested_price,
                filled_price, slippage, volume_requested, volume_filled, status,
                retcode, rejection_reason, order_ticket, position_ticket, intent_id,
                precision,
                json.dumps(metadata or {}, sort_keys=True, default=str),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def read_execution_ledger(db_path: str, asset_key: str | None = None) -> pd.DataFrame:
    init_execution_ledger(db_path)
    query = f"SELECT * FROM {TABLE_NAME}"
    params: list[object] = []
    if asset_key is not None:
        query += " WHERE asset_key = ?"
        params.append(asset_key)
    query += " ORDER BY requested_at_ms, id"
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def broker_spread_report(db_path: str, timeframe: str, asset_key: str) -> dict:
    """Summarize broker-reported bar spread (raw MT5 points, never mislabeled USD)."""
    candles = read_candles(db_path, timeframe, asset_key)
    spread = pd.to_numeric(candles.get("spread"), errors="coerce").dropna()
    real_volume = pd.to_numeric(candles.get("real_volume"), errors="coerce").dropna()
    if spread.empty:
        return {
            "asset_key": asset_key, "timeframe": timeframe,
            "observations": 0, "unit": "broker_points",
        }
    return {
        "asset_key": asset_key,
        "timeframe": timeframe,
        "observations": int(len(spread)),
        "unit": "broker_points",
        "spread": {
            f"p{int(q * 100):02d}": float(spread.quantile(q))
            for q in (0.5, 0.9, 0.95, 0.99)
        },
        "real_volume_observations": int(len(real_volume)),
    }


def execution_cost_report(db_path: str, asset_key: str | None = None) -> dict:
    """Return fill/rejection counts and empirical latency/slippage percentiles."""
    df = read_execution_ledger(db_path, asset_key)
    if df.empty:
        return {"asset_key": asset_key, "attempts": 0, "fills": 0, "rejections": 0}
    fills = df[df["status"].isin(["filled", "partial"])]

    def _percentiles(series: pd.Series) -> dict:
        valid = pd.to_numeric(series, errors="coerce").dropna()
        if valid.empty:
            return {}
        return {f"p{int(q * 100):02d}": float(valid.quantile(q)) for q in (0.5, 0.9, 0.95, 0.99)}

    return {
        "asset_key": asset_key,
        "attempts": int(len(df)),
        "fills": int(len(fills)),
        "rejections": int((df["status"] == "rejected").sum()),
        "rejection_rate": float((df["status"] == "rejected").mean()),
        "latency_ms": _percentiles(df["latency_ms"]),
        "adverse_slippage_price_units": _percentiles(fills["adverse_slippage"]),
        "from_ms": int(df["requested_at_ms"].min()),
        "to_ms": int(df["completed_at_ms"].max()),
    }
