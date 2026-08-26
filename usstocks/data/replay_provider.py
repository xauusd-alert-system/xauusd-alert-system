"""Offline CSV replay provider — zero network I/O (ТЗ §11 replay profile).

Expected CSV columns: symbol,ts,open,high,low,close,volume
`ts` is ISO-8601 (naive treated as America/New_York; aware kept as-is) or a
UNIX epoch in seconds.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from usstocks.indicators import ensure_ny
from usstocks.models import Bar


def _parse_ts(raw: str) -> datetime:
    raw = raw.strip()
    if raw.replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(raw), tz=ensure_ny(datetime.now()).tzinfo)
    ts = datetime.fromisoformat(raw)
    return ensure_ny(ts)


def load_bars(csv_path: str | Path, symbol: str = None) -> List[Bar]:
    """Load one CSV into closed Bar list, oldest first."""
    bars: List[Bar] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sym = (row.get("symbol") or "").strip()
            if symbol and sym and sym.upper() != symbol.upper():
                continue
            bars.append(Bar(
                ts=_parse_ts(row["ts"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            ))
    bars.sort(key=lambda b: b.ts)
    return bars


def load_universe(paths: Dict[str, str | Path]) -> Dict[str, List[Bar]]:
    """Load several per-symbol CSVs at once: {symbol: path}."""
    return {sym.upper(): load_bars(p, sym) for sym, p in paths.items()}
