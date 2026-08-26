"""One-off: regenerate provenance manifests for every (asset, timeframe) combo
in the rebuilt (true-UTC) market_data DB.

Runs after scripts/rebuild_db_utc.py so the frozen data_hash / candle_count /
gap_audit reflect the true-UTC timestamps. Safe to re-run (idempotent overwrite).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from config.loader import load_config
from data.provenance import build_provenance_manifest, write_provenance_manifest
from data.storage import get_connection

DB = "data/market_data_mt5.sqlite"
BROKER = "FxPro"
OUT_DIR = Path("config/provenance")


def main() -> None:
    cfg = load_config()
    offset = float((cfg.get("market_data", {}) or {}).get("server_time_offset_hours", 0.0))
    sessions = cfg.get("sessions", {}) or {}

    conn = get_connection(DB)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ohlcv\\_%' ESCAPE '\\'"
    ).fetchall() if r[0].startswith("ohlcv_")]
    conn.close()

    combos: list[tuple[str, str]] = []
    for table in sorted(tables):
        tf = table.replace("ohlcv_", "").upper()
        conn = get_connection(DB)
        symbols = [r[0] for r in conn.execute(f"SELECT DISTINCT symbol FROM {table}").fetchall()]
        conn.close()
        for sym in sorted(symbols):
            combos.append((tf, sym))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for tf, asset in combos:
        broker_symbol = (cfg.get("assets", {}).get(asset, {}) or {}).get("mt5_symbol", asset)
        out = OUT_DIR / f"{asset}_{tf}_fxpro.json"
        manifest = build_provenance_manifest(
            DB, tf, asset,
            broker=BROKER,
            broker_symbol=broker_symbol,
            terminal_company_hash=None,
            terminal_build=None,
            terminal_data_path_hash=None,
            server_time_offset_hours=offset,
            sessions_config=sessions,
            extra={
                "command": "python scripts/rebuild_provenance_manifests.py",
                "broker_symbol_input": broker_symbol,
                "note": "Regenerated after true-UTC DB rebuild (server_time_offset_hours=%s)" % offset,
            },
        )
        write_provenance_manifest(str(out), manifest)
        results.append({
            "asset": asset, "timeframe": tf, "out": str(out),
            "candle_count": manifest["candle_count"],
            "data_hash": manifest["data_hash"],
            "gap_audit": manifest["gap_audit"],
        })
        print(f"OK {asset} {tf}: {manifest['candle_count']} bars hash={manifest['data_hash'][:12]}", flush=True)

    print("SUMMARY", json.dumps([{k: r[k] for k in ("asset", "timeframe", "candle_count", "data_hash")} for r in results]))


if __name__ == "__main__":
    main()
