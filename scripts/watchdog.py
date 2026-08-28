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

import logging
import os
import signal
import subprocess
import sys
import time
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
# Audit 2026-08-23 D: cooldown grows exponentially between consecutive fast
# crashes so a broken terminal isn't hammered every 10s.
MAX_BACKOFF_SECS = int(os.getenv("WATCHDOG_MAX_BACKOFF_SECS", "60"))
# Audit A: heartbeat refresh interval while the trader is healthy.
HEARTBEAT_SECS = int(os.getenv("WATCHDOG_HEARTBEAT_SECS", "30"))

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


# ---------------------------------------------------------------------------
# Single-instance guard: a second watchdog would spawn a second trader, and
# both traders would fight over one MT5 account AND double-poll the Telegram
# bot token (409 Conflict on getUpdates). The lock records our PID; if that
# PID is still alive as a python process, refuse to start.
# ---------------------------------------------------------------------------
LOCK_FILE = os.path.join(PROJECT_ROOT, "logs", "watchdog.lock")


def _pid_alive_as_python(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return ('"pythonw.exe"' in out) or ('"python.exe"' in out)
    except Exception:
        return False


def _acquire_single_instance_lock() -> bool:
    """Returns True if we may run; False if another live watchdog holds the lock."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid() and _pid_alive_as_python(old_pid):
                log.warning(
                    "Another watchdog appears alive (pid=%s from %s). Exiting "
                    "to avoid a duplicate trader / Telegram 409 conflict.",
                    old_pid, LOCK_FILE,
                )
                return False
        except (ValueError, OSError):
            pass  # stale/corrupt lock - overwrite below
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def _cooldown_for(crashes: int) -> float:
    """Audit D: exponential cooldown between consecutive fast crashes."""
    return min(float(COOLDOWN_SECS) * (2 ** max(0, crashes - 1)), float(MAX_BACKOFF_SECS))


def _tg(text: str) -> None:
    """Audit C: fire-and-forget Telegram notify (send-only, no polling — no
    getUpdates conflict). Used for crash-restart and give-up visibility: when
    the trader is down, its own bot can't tell you anything."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if not token or not chat:
        return
    try:
        import requests  # local import: watchdog stays stdlib-only at startup
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text}, timeout=10,
        )
    except Exception as exc:
        log.warning("Telegram notify failed: %s", exc)


def _launch_trader() -> subprocess.Popen:
    """Start the trader as a subprocess, inheriting the current env."""
    env = os.environ.copy()
    # Ensure stdout/stderr from the child go to their own log files
    trader_log = open(os.path.join(PROJECT_ROOT, "logs", "trader_stdout.log"), "a", encoding="utf-8")
    trader_err = open(os.path.join(PROJECT_ROOT, "logs", "trader_stderr.log"), "a", encoding="utf-8")

    try:
        # Audit B: close the parent-side handles right after spawn — the child
        # keeps its inherited copies. Previously they leaked per restart.
        return subprocess.Popen(
            [sys.executable, "-u", TRADER_PATH],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=trader_log,
            stderr=trader_err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console popup on Windows
        )
    finally:
        trader_log.close()
        trader_err.close()


def main():
    global consecutive_crashes, shutdown_requested

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if not _acquire_single_instance_lock():
        return

    log.info(f"Watchdog starting — trader={TRADER_PATH}")
    log.info(f"Max restarts={MAX_RESTARTS}, cooldown={COOLDOWN_SECS}s, stable_after={STABLE_SECONDS}s")

    while not shutdown_requested:
        if consecutive_crashes >= MAX_RESTARTS:
            log.error(f"Hit max consecutive crashes ({MAX_RESTARTS}). Watchdog stopping.")
            _tg(f"⛔ Watchdog сдался после {MAX_RESTARTS} рестартов подряд. "
                f"Трейдер НЕ работает — нужен ручной запуск.")
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
        _write_heartbeat(proc.pid)

        # Block until trader exits; audit A: refresh the heartbeat while
        # healthy so external monitors can detect watchdog liveness.
        returncode = -1
        while True:
            try:
                returncode = proc.wait(timeout=HEARTBEAT_SECS)
                break
            except subprocess.TimeoutExpired:
                _write_heartbeat(proc.pid)
            except Exception:
                returncode = -1
                break

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
            # Audit C: a fast crash is abnormal — make it visible in Telegram.
            _tg(f"🔄 Trader упал (code={returncode}, uptime={uptime:.0f}s). "
                f"Рестарт {consecutive_crashes}/{MAX_RESTARTS}...")

        _write_heartbeat(proc.pid)

        # Cooldown before restart — audit D: exponential on consecutive fast
        # crashes (10s -> 20s -> ... capped), plain cooldown after healthy runs.
        remaining = COOLDOWN_SECS - uptime
        if consecutive_crashes > 0:
            remaining = max(remaining, _cooldown_for(consecutive_crashes))
        if remaining > 0:
            log.info(f"Cooling down {remaining:.0f}s before restart...")
            time.sleep(remaining)

    # Cleanup
    log.info("Watchdog shutting down gracefully.")
    _write_heartbeat(0)


if __name__ == "__main__":
    main()
