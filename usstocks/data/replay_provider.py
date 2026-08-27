"""Offline CSV replay provider — zero network I/O (ТЗ §11 replay profile).

Expected CSV columns: symbol,ts,open,high,low,close,volume
`ts` is ISO-8601 (naive treated as America/New_York; aware kept as-is) or a
UNIX epoch in seconds.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from usstocks.indicators import ensure_ny
from usstocks.models import Bar

REQUIRED_CSV_COLUMNS: Set[str] = {"ts", "open", "high", "low", "close"}


def _parse_ts(raw: str, row_idx: int = 0) -> datetime:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"Row {row_idx}: empty timestamp")
    try:
        if raw.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(raw), tz=ensure_ny(datetime.now()).tzinfo)
        ts = datetime.fromisoformat(raw)
        return ensure_ny(ts)
    except Exception as e:
        raise ValueError(f"Row {row_idx}: invalid timestamp {raw!r}: {e}") from e


def load_bars(csv_path: str | Path, symbol: Optional[str] = None) -> List[Bar]:
    """Load one CSV into closed Bar list, oldest first, with strict validation."""
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    bars: List[Bar] = []
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV {csv_path} is empty or has no header")

        fieldnames_lower = {name.strip().lower(): name for name in reader.fieldnames if name}
        missing = [col for col in REQUIRED_CSV_COLUMNS if col not in fieldnames_lower]
        if missing:
            raise ValueError(
                f"CSV {csv_path} missing required columns: {sorted(missing)}. "
                f"Found: {sorted(list(fieldnames_lower.keys()))}"
            )

        col_ts = fieldnames_lower["ts"]
        col_open = fieldnames_lower["open"]
        col_high = fieldnames_lower["high"]
        col_low = fieldnames_lower["low"]
        col_close = fieldnames_lower["close"]
        col_vol = fieldnames_lower.get("volume")
        col_sym = fieldnames_lower.get("symbol")

        for idx, row in enumerate(reader, start=2):
            if not row or all(v is None or not str(v).strip() for v in row.values()):
                continue  # Skip blank lines

            if col_sym and symbol:
                sym = (row.get(col_sym) or "").strip()
                if sym and sym.upper() != symbol.upper():
                    continue

            try:
                ts = _parse_ts(row[col_ts], row_idx=idx)
                o = float(row[col_open])
                h = float(row[col_high])
                l = float(row[col_low])
                c = float(row[col_close])
                v = float(row[col_vol]) if (col_vol and row.get(col_vol)) else 0.0
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f"CSV {csv_path} row {idx} parse error: {e}") from e

            bars.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v))

    if not bars and symbol is None:
        raise ValueError(f"CSV {csv_path} contains no valid data rows")

    bars.sort(key=lambda b: b.ts)
    return bars


def load_universe(paths: Dict[str, str | Path]) -> Dict[str, List[Bar]]:
    """Load several per-symbol CSVs at once: {symbol: path}."""
    return {sym.upper(): load_bars(p, sym) for sym, p in paths.items()}


def dump_bars(bars: List[Bar], csv_path: str | Path, symbol: Optional[str] = None) -> None:
    """Write list of Bar objects to a CSV file."""
    p = Path(csv_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["symbol", "ts", "open", "high", "low", "close", "volume"] if symbol else ["ts", "open", "high", "low", "close", "volume"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for b in bars:
            row = {
                "ts": b.ts.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            if symbol:
                row["symbol"] = symbol
            writer.writerow(row)
