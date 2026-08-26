"""Reconcile live MT5 trades (magic 777111) with the executed_trades ledger.

Read-only: connects to the running MT5 terminal, reads account info, open
positions and deal history filtered by magic 777111, then joins against the
SQLite executed_trades ledger by ticket and compares PnL totals.

Deal timestamps from MT5 are in SERVER time; the resolved config offset is
subtracted to compare against true-UTC ledger timestamps.
"""
import os
import sys
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config

MAGIC = 777111
START_UTC = "2026-08-25 00:00:00"  # compare today's trades


def _server_offset() -> float:
    from data.mt5_provider import resolve_server_offset
    cfg = load_config()
    return resolve_server_offset(cfg.get("market_data", {}))


def main() -> None:
    cfg = load_config()
    db = cfg["general"]["db_path"]
    offset = _server_offset()
    print(f"server offset resolved: {offset}h")

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.account_info()
    print(f"account: {info.login} | balance={info.balance:.2f} equity={info.equity:.2f} "
          f"floating={info.profit:.2f} | server={info.server}")

    # --- open positions (magic filtered in python; MT5 API has no magic arg) ---
    positions = [p for p in (mt5.positions_get() or []) if getattr(p, "magic", None) == MAGIC]
    print(f"\nopen positions (magic {MAGIC}): {len(positions)}")
    for p in positions:
        print(f"  ticket={p.ticket} {p.symbol} {'buy' if p.type == 0 else 'sell'} vol={p.volume} "
              f"open={p.price_open} now={p.price_current} profit={p.profit:.2f}")

    # --- deal history (server time!) since START_UTC - margin ---
    start_true = datetime.strptime(START_UTC, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    from_server = int(start_true.timestamp()) + int(offset * 3600) - 3600  # 1h margin
    to_server = int(datetime.now(timezone.utc).timestamp()) + int(offset * 3600) + 3600
    deals = mt5.history_deals_get(from_server, to_server) or []

    # Positions opened with OUR magic. Closing deals may carry a different magic
    # (0 = manual terminal close, 999001 = another EA on this shared demo), so
    # ALL deals per position are netted — profit + commission + swap — matching
    # the trader's broker-net PnL formula (execution/mt5_trader.py:879).
    our_positions = {d.position_id for d in deals if getattr(d, "magic", None) == MAGIC}
    print(f"\nMT5 deals since {START_UTC}: {len(deals)} | positions opened with magic {MAGIC}: {len(our_positions)}")

    from collections import defaultdict
    by_pos = defaultdict(list)
    for d in deals:
        if d.position_id in our_positions:
            by_pos[d.position_id].append(d)

    mt5_rows = []
    for pos_id, ds in by_pos.items():
        net = sum((getattr(d, "profit", 0.0) or 0.0)
                  + (getattr(d, "commission", 0.0) or 0.0)
                  + (getattr(d, "swap", 0.0) or 0.0) for d in ds)
        symbol = ds[0].symbol
        entry = min(d.time for d in ds)
        exit_t = max(d.time for d in ds)
        # deal.time is server time -> true UTC
        mt5_rows.append({
            "ticket": pos_id, "symbol": symbol,
            "entry_utc": datetime.fromtimestamp(entry - offset * 3600, timezone.utc),
            "exit_utc": datetime.fromtimestamp(exit_t - offset * 3600, timezone.utc),
            "n_deals": len(ds), "pnl": round(net, 2),
        })
    mt5_df = pd.DataFrame(mt5_rows)
    if not mt5_df.empty:
        print(f"MT5 closed positions (opened by us): {len(mt5_df)} | total net PnL = {mt5_df['pnl'].sum():.2f}")
    else:
        print("MT5: no positions opened with our magic in window")
    mt5.shutdown()

    # --- ledger ---
    import sqlite3
    conn = sqlite3.connect(db)
    led = pd.read_sql_query(
        "SELECT ticket, symbol, bias, entry_time, entry_price, close_time, close_price, pnl, outcome "
        "FROM executed_trades", conn)
    led["entry_utc"] = pd.to_datetime(led["entry_time"], unit="s", utc=True)
    led_today = led[led["entry_utc"] >= start_true]
    print(f"\nledger executed_trades: {len(led)} total | today: {len(led_today)}")
    if not led_today.empty:
        print(f"ledger today PnL = {led_today['pnl'].sum():.2f} | per symbol:")
        print(led_today.groupby("symbol")["pnl"].agg(["count", "sum"]).to_string())

    # --- ticket join ---
    if not mt5_df.empty and not led_today.empty:
        mt5_tickets = set(mt5_df["ticket"].astype(int))
        led_tickets = set(led_today["ticket"].astype(int))
        only_mt5 = sorted(mt5_tickets - led_tickets)
        only_led = sorted(led_tickets - mt5_tickets)
        common = mt5_tickets & led_tickets
        print(f"\njoin: common={len(common)} | only in MT5={len(only_mt5)} | only in ledger={len(only_led)}")
        if only_mt5:
            print("  MT5-only tickets (ledger missing):", only_mt5[:10])
        if only_led:
            print("  ledger-only tickets (MT5 missing):", only_led[:10])

        m = mt5_df.set_index("ticket")["pnl"]
        l = led_today.set_index("ticket")["pnl"]
        joined = pd.DataFrame({"mt5_pnl": m, "ledger_pnl": l}).dropna()
        diff = (joined["mt5_pnl"] - joined["ledger_pnl"]).abs()
        print(f"PnL per common ticket: max abs diff = {diff.max():.4f} | "
              f"total MT5-net {joined['mt5_pnl'].sum():.2f} vs ledger {joined['ledger_pnl'].sum():.2f}")
        bad = joined[diff > 0.01]
        if not bad.empty:
            print("tickets with diff > 0.01:")
            print(bad.to_string())

    print(f"\nTOTALS: MT5 net (all closes) = {mt5_df['pnl'].sum() if not mt5_df.empty else 0:.2f} | "
          f"ledger today = {led_today['pnl'].sum() if not led_today.empty else 0:.2f} | "
          f"delta = {(mt5_df['pnl'].sum() if not mt5_df.empty else 0) - (led_today['pnl'].sum() if not led_today.empty else 0):.2f}")


if __name__ == "__main__":
    main()
