#!/usr/bin/env python3
"""
Watchdog: auto-restarts execution/mt5_trader.py if it dies.

Usage:
    python scripts/watchdog.py              # foreground
    pythonw scripts/watchdog.py             # background (Windows)

Env vars:
    WATCHDOG_MAX_RESTARTS   — give up after N consecutive fast crashes (default 20)
    WATCHDOG_COOLDOWN_SECS  — minimum seconds before restart after a crash (default 10)
    WATCHDOG_HEALTH_FILE    — touched every successful heartbeat (default logs/watchdog_heartbeat.json)
"""

import os
import sys
import time
import signal
import subprocess
import logging
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_PATH = os.path.join(PROJECT_ROOT, "execution", "mt5_trader.py")
HEALTH_FILE = os.getenv(
    "WATCHDOG_HEALTH_FILE",
    os.path.join(PROJECT_ROOT, "logs", "watchdog_heartbeat.json"),
)
MAX_RESTARTS = int(os.getenv("WATCHDOG_MAX_RESTARTS", "20"))
COOLDOWN_SECS = int(os.getenv("WATCHDOG_COOLDOWN_SECS", "10"))
# If the trader runs longer than this without crashing, reset the crash counter
STABLE_SECONDS = int(os.getenv("WATCHDOG_STABLE_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
os.makedirs(os.path.join(PROJECT_ROOT, "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(PROJECT_ROOT, "logs", "watchdog.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("watchdog")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
consecutive_crashes = 0
shutdown_requested = False


def _write_heartbeat(pid: int):
    """Touch a health file so external monitors can confirm the watchdog is alive."""
    import json
    os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
    with open(HEALTH_FILE, "w") as f:
        json.dump(
            {
                "watchdog_pid": os.getpid(),
                "trader_pid": pid,
                "ts": datetime.now(timezone.utc).isoformat(),
                "consecutive_crashes": consecutive_crashes,
            },
            f,
        )


def _signal_handler(signum, frame):
    global shutdown_requested
    log.info(f"Received signal {signum}, shutting down...")
    shutdown_requested = True


def _launch_trader() -> subprocess.Popen:
    """Start the trader as a subprocess, inheriting the current env."""
    env = os.environ.copy()
    # Ensure stdout/stderr from the child go to their own log files
    trader_log = open(os.path.join(PROJECT_ROOT, "logs", "trader_stdout.log"), "a", encoding="utf-8")
    trader_err = open(os.path.join(PROJECT_ROOT, "logs", "trader_stderr.log"), "a", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-u", TRADER_PATH],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=trader_log,
        stderr=trader_err,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console popup on Windows
    )
    return proc


def main():
    global consecutive_crashes, shutdown_requested

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log.info(f"Watchdog starting — trader={TRADER_PATH}")
    log.info(f"Max restarts={MAX_RESTARTS}, cooldown={COOLDOWN_SECS}s, stable_after={STABLE_SECONDS}s")

    while not shutdown_requested:
        if consecutive_crashes >= MAX_RESTARTS:
            log.error(f"Hit max consecutive crashes ({MAX_RESTARTS}). Watchdog stopping.")
            break

        log.info(f"Launching trader (attempt #{consecutive_crashes + 1})...")
        try:
            proc = _launch_trader()
        except Exception as e:
            log.error(f"Failed to launch trader: {e}")
            consecutive_crashes += 1
            time.sleep(COOLDOWN_SECS)
            continue

        log.info(f"Trader started — PID={proc.pid}")
        start_time = time.time()

        # Block until trader exits
        try:
            returncode = proc.wait()
        except Exception:
            returncode = -1

        uptime = time.time() - start_time
        log.info(f"Trader exited — code={returncode}, uptime={uptime:.0f}s")

        if shutdown_requested:
            break

        # If the trader ran long enough, it was a healthy run — reset crash counter
        if uptime >= STABLE_SECONDS:
            log.info(f"Trader ran for {uptime:.0f}s (>= {STABLE_SECONDS}s) — resetting crash counter")
            consecutive_crashes = 0
        else:
            consecutive_crashes += 1

        _write_heartbeat(proc.pid)

        # Cooldown before restart
        remaining = COOLDOWN_SECS - uptime
        if remaining > 0:
            log.info(f"Cooling down {remaining:.0f}s before restart...")
            time.sleep(remaining)

    # Cleanup
    log.info("Watchdog shutting down gracefully.")
    _write_heartbeat(0)


if __name__ == "__main__":
    main()
