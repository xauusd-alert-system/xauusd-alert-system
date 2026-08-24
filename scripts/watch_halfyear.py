import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "data" / "backtest" / "halfyear_results.json"
LOG = ROOT / "logs" / "halfyear.log"
sys.path.insert(0, str(ROOT))


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"[watcher] {message}\n")


def notify(data):
    lines = ["✅ Полугодовой прогон готов:"]
    for rr in ("3.5", "0.85"):
        if rr in data:
            result = data[rr]
            lines.append(
                f"RR {rr}: n={result.get('n')} WR={result.get('wr')}% "
                f"avgR={result.get('avgR')} sumR={result.get('sumR')}"
            )
    try:
        from challenge.manual.alerter import tg_send
        tg_send("\n".join(lines))
        log("notified")
    except Exception as exc:
        log(f"notification failed: {exc}")


def main():
    initial_mtime = RESULT.stat().st_mtime if RESULT.exists() else 0
    for _ in range(720):
        time.sleep(30)
        if not RESULT.exists() or RESULT.stat().st_mtime <= initial_mtime:
            continue
        try:
            with RESULT.open(encoding="utf-8") as stream:
                notify(json.load(stream))
            return 0
        except Exception as exc:
            log(f"read failed: {exc}")
            time.sleep(10)
    log("timeout waiting for result")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
