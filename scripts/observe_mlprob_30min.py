"""30-minute live observer: /api/ml-prob freshness + trader signal-cycle health.

Every ~60s for `--minutes` minutes:
  * GET /api/ml-prob?asset=XAUUSD and record the newest bar ts / p_long / p_short,
    plus the endpoint's `as_of_utc` so staleness is measurable.
  * Tail logs/trader_real.log for new lines; flag ERROR/CRITICAL/Traceback and
    count signal-cycle markers (New bar detected / Analyzing newly closed candle /
    no trade / Waiting for new bar).

Writes a compact summary to stdout and a full CSV to logs/mlprob_observe.csv.

Usage:
    python -m scripts.observe_mlprob_30min --minutes 30
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "http://127.0.0.1:8000"
LOG = Path("logs/trader_real.log")
CSV = Path("logs/mlprob_observe.csv")

ERROR_TOKENS = ("ERROR", "CRITICAL", "Traceback", "Exception", " FAILED")
CYCLE_TOKENS = ("New bar detected", "Analyzing newly closed candle", "no trade", "Waiting for new bar", "BE CHECK")


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def fetch_mlprob(asset: str = "XAUUSD") -> dict:
    url = f"{BASE}/api/ml-prob?asset={asset}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}
    hist = d.get("history") or []
    last = hist[-1] if hist else {}
    return {
        "available": d.get("available"),
        "status": d.get("status"),
        "n_bars": len(hist),
        "last_ts": last.get("ts"),
        "last_price": last.get("price"),
        "last_p_long": last.get("p_long"),
        "last_p_short": last.get("p_short"),
        "last_regime": last.get("regime"),
        "as_of_utc": d.get("as_of_utc"),
        "error": None,
    }


def scan_log(last_pos: int):
    """Return (new_lines, new_errors, cycle_count, log_pos)."""
    if not LOG.exists():
        return [], 0, 0, last_pos
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        f.seek(last_pos)
        lines = f.read().splitlines()
    errs = [ln for ln in lines if any(t in ln for t in ERROR_TOKENS)]
    cyc = sum(1 for ln in lines if any(t in ln for t in CYCLE_TOKENS))
    return lines, len(errs), cyc, last_pos + sum(len(l) + 1 for l in lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()

    total = args.minutes * 60
    start = time.time()
    pos = LOG.stat().st_size if LOG.exists() else 0
    errors_seen = []
    cycles_seen = 0
    samples = []

    print(f"observer started {_now()}Z for {args.minutes} min (interval {args.interval}s)")

    while time.time() - start < total:
        ts = _now()
        m = fetch_mlprob()
        lines, n_err, n_cyc, pos = scan_log(pos)
        cycles_seen += n_cyc
        if n_err:
            errors_seen.extend(lines)
        row = {"ts_utc": ts, **m, "new_log_lines": len(lines), "new_errors": n_err, "new_cycle_markers": n_cyc}
        samples.append(row)
        with open(CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            w.writeheader()
            w.writerows(samples)
        bar_ts = row.get("last_ts")
        bar_age = ""
        if bar_ts and m.get("as_of_utc"):
            try:
                age = float(m["as_of_utc"]) - float(bar_ts) if False else None
            except Exception:
                age = None
            bar_age = f" bar_age~{age}s" if age is not None else ""
        print(
            f"[{ts}Z] bars={row.get('n_bars')} last_ts={bar_ts}{bar_age} "
            f"p_long={row.get('last_p_long')} regime={row.get('last_regime')} "
            f"avail={row.get('available')} log+={len(lines)} err+={n_err} cyc+={n_cyc}",
            flush=True,
        )
        time.sleep(args.interval)

    print("\n=== 30-min summary ===")
    print(f"samples={len(samples)}  cycle_markers={cycles_seen}  total_errors={len(errors_seen)}")
    if errors_seen:
        print("ERRORS:")
        for e in errors_seen[-10:]:
            print("  " + e[:200])
    else:
        print("no ERROR/CRITICAL/Traceback lines in trader log during observation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
