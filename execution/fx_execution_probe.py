"""Safe, demo-only FX execution probe.

This utility measures actual FxPro demo market-order costs without involving the
ML strategy: it opens one minimum-volume EURUSD/GBPUSD position and closes it
within a few seconds, recording pre/post quotes, requested and filled prices,
retcodes and realized PnL in a CSV. It refuses real accounts, existing positions
on the symbol, and execution without two explicit confirmations.
"""
import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from data.mt5_provider import initialize_mt5, shutdown_mt5, validate_symbol

PROBE_MAGIC = 777222
ALLOWED_ASSETS = {"EURUSD", "GBPUSD"}
CSV_FIELDS = [
    "timestamp_utc", "asset", "symbol", "side", "volume", "entry_bid", "entry_ask",
    "entry_requested_price", "entry_fill_price", "entry_retcode", "entry_comment",
    "close_bid", "close_ask", "close_requested_price", "close_fill_price",
    "close_retcode", "close_comment", "position_ticket", "entry_commission",
    "close_commission", "total_commission", "realized_profit", "status",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_demo_account() -> bool:
    account = mt5.account_info()
    demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
    return bool(account is not None and demo_mode is not None and
                getattr(account, "trade_mode", None) == demo_mode)


def _append_row(path: str, row: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    new_file = not target.exists()
    with target.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow({key: row.get(key) for key in CSV_FIELDS})


def _result_fields(result, prefix: str) -> dict:
    return {
        f"{prefix}_fill_price": getattr(result, "price", None),
        f"{prefix}_retcode": getattr(result, "retcode", None),
        f"{prefix}_comment": getattr(result, "comment", None),
    }


def _open_positions(symbol: str):
    return mt5.positions_get(symbol=symbol) or []


def execute_probe(asset: str, side: str, volume: float, hold_seconds: float,
                  csv_path: str, execute: bool) -> dict:
    """Run one deliberately short, independently logged demo execution probe."""
    if asset not in ALLOWED_ASSETS:
        raise ValueError(f"asset must be one of {sorted(ALLOWED_ASSETS)}, got {asset!r}")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if not (0 < hold_seconds <= 5):
        raise ValueError("hold_seconds must be > 0 and <= 5 for a cost probe")
    if execute and os.getenv("FX_PROBE_CONFIRM") != "YES":
        raise RuntimeError("Refusing to send an order. Set FX_PROBE_CONFIRM=YES explicitly.")

    initialize_mt5()
    try:
        if not _is_demo_account():
            raise RuntimeError("Refusing FX probe: connected MT5 account is not DEMO.")
        validate_symbol(asset)
        info = mt5.symbol_info(asset)
        if info is None:
            raise RuntimeError(f"Cannot read specification for {asset}")
        if _open_positions(asset):
            raise RuntimeError(f"Refusing probe: an open {asset} position already exists.")
        if volume < float(info.volume_min) - 1e-12:
            raise ValueError(f"volume {volume} is below broker minimum {info.volume_min}")

        tick = mt5.symbol_info_tick(asset)
        if tick is None:
            raise RuntimeError(f"No quote for {asset}")
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        entry_price = tick.ask if side == "buy" else tick.bid
        row = {
            "timestamp_utc": _utc_now(), "asset": asset, "symbol": asset,
            "side": side, "volume": volume, "entry_bid": tick.bid, "entry_ask": tick.ask,
            "entry_requested_price": entry_price, "status": "dry_run" if not execute else "pending",
        }
        if not execute:
            _append_row(csv_path, row)
            return row

        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": asset, "volume": volume,
            "type": order_type, "price": entry_price, "deviation": 20,
            "magic": PROBE_MAGIC, "comment": "FX_COST_PROBE", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        entry = mt5.order_send(request)
        row.update(_result_fields(entry, "entry"))
        if getattr(entry, "retcode", None) != mt5.TRADE_RETCODE_DONE:
            row["status"] = "entry_rejected"
            _append_row(csv_path, row)
            return row

        positions = _open_positions(asset)
        probe_positions = [p for p in positions if getattr(p, "magic", None) == PROBE_MAGIC]
        if len(probe_positions) != 1:
            row["status"] = "entry_unresolved"
            _append_row(csv_path, row)
            raise RuntimeError("Probe entry filled but its position could not be resolved; close it in MT5.")
        position = probe_positions[0]
        row["position_ticket"] = position.ticket
        time.sleep(hold_seconds)

        close_tick = mt5.symbol_info_tick(asset)
        close_type = mt5.ORDER_TYPE_SELL if side == "buy" else mt5.ORDER_TYPE_BUY
        close_price = close_tick.bid if side == "buy" else close_tick.ask
        row.update({"close_bid": close_tick.bid, "close_ask": close_tick.ask,
                    "close_requested_price": close_price})
        close = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": asset, "volume": float(position.volume),
            "type": close_type, "position": position.ticket, "price": close_price,
            "deviation": 20, "magic": PROBE_MAGIC, "comment": "FX_COST_PROBE_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        })
        row.update(_result_fields(close, "close"))
        row["status"] = "closed" if getattr(close, "retcode", None) == mt5.TRADE_RETCODE_DONE else "close_rejected"
        # The order result itself has no commission. Read the terminal's deals
        # after a successful close so the CSV captures the broker's actual cost,
        # rather than inferring it from a contract specification.
        if row["status"] == "closed":
            try:
                deals = mt5.history_deals_get(position=position.ticket) or []
                commissions = [float(getattr(d, "commission", 0.0) or 0.0) for d in deals]
                profits = [float(getattr(d, "profit", 0.0) or 0.0) for d in deals]
                if commissions:
                    row["entry_commission"] = commissions[0]
                    row["close_commission"] = commissions[-1]
                    row["total_commission"] = sum(commissions)
                if profits:
                    row["realized_profit"] = sum(profits) + sum(commissions)
            except Exception:
                # Fill and retcode data are still valuable if a broker delays
                # deal-history availability for a moment.
                pass
        _append_row(csv_path, row)
        return row
    finally:
        shutdown_mt5()


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo-only EURUSD/GBPUSD execution-cost probe.")
    parser.add_argument("--asset", choices=sorted(ALLOWED_ASSETS), required=True)
    parser.add_argument("--side", choices=["buy", "sell"], required=True)
    parser.add_argument("--volume", type=float, default=0.01)
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    parser.add_argument("--out", default="logs/fx_execution_probes.csv")
    parser.add_argument("--execute", action="store_true", help="Actually send one demo probe order.")
    args = parser.parse_args()
    row = execute_probe(args.asset, args.side, args.volume, args.hold_seconds, args.out, args.execute)
    print(f"[fx-probe] {row['status']} → {args.out}")


if __name__ == "__main__":
    main()
