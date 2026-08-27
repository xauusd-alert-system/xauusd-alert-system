"""
Safe process manager for trader/dashboard lifecycle.

Core problem solved: when restarting the trader, naive PID-based killing
(e.g. PowerShell Stop-Process -Id A,B) can accidentally kill the dashboard
if both PIDs are passed together.  This module uses command-line pattern
matching so it ONLY touches the intended target.

Usage
-----
    python -m scripts.process_manager status
    python -m scripts.process_manager kill-trader
    python -m scripts.process_manager restart-trader
    python -m scripts.process_manager kill-dashboard
    python -m scripts.process_manager restart-dashboard

Also importable for Telegram admin bot:
    from scripts.process_manager import kill_trader, restart_trader, get_status
"""
import argparse
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("process_manager")

# ---------------------------------------------------------------------------
# Patterns: command-line substrings that identify each role
# ---------------------------------------------------------------------------
TRADER_PATTERNS = ["execution.mt5_trader", "challenge.runner"]
DASHBOARD_PATTERNS = ["uvicorn realtime.app"]

# ---------------------------------------------------------------------------
# Python interpreter detection
# ---------------------------------------------------------------------------
_VENV_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "venv", "Scripts", "python.exe")
)
_SYSTEM_PYTHON = sys.executable  # the interpreter running this script


def _is_venv_python(path: str) -> bool:
    """Return True if *path* resolves to the project venv interpreter."""
    try:
        return os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(_VENV_PYTHON)
        )
    except Exception:
        return False


def _python_label(path: str) -> str:
    if _is_venv_python(path):
        return "venv"
    return "system"


# ---------------------------------------------------------------------------
# Process discovery (Windows-only via WMIC; falls back to tasklist)
# ---------------------------------------------------------------------------
def _discover_processes() -> list[dict]:
    """Return list of {pid, exe, cmdline_parts, python_path, role} dicts."""
    rows = []
    try:
        raw = subprocess.check_output(
            [
                "powershell", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select-Object ProcessId,CommandLine | Format-List",
            ],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return rows

    pid = None
    cmdline = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("ProcessId"):
            pid = int(line.split(":", 1)[1].strip())
        elif line.startswith("CommandLine"):
            cmdline = line.split(":", 1)[1].strip()

        if pid is not None and cmdline:
            parts = cmdline.split()
            python_path = parts[0] if parts else ""
            # Determine role
            role = "unknown"
            cmd_lower = cmdline.lower()
            for pat in TRADER_PATTERNS:
                if pat.lower() in cmd_lower:
                    role = "trader"
                    break
            if role == "unknown":
                for pat in DASHBOARD_PATTERNS:
                    if pat.lower() in cmd_lower:
                        role = "dashboard"
                        break
            rows.append({
                "pid": pid,
                "exe": python_path,
                "python_label": _python_label(python_path),
                "role": role,
                "cmdline": cmdline,
            })
            pid = None
            cmdline = ""
    return rows


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def get_status() -> str:
    """Human-readable status of all Python processes."""
    procs = _discover_processes()
    if not procs:
        return "No Python processes found."
    lines = []
    for p in procs:
        tag = "*" if p["role"] != "unknown" else "?"
        lines.append(
            f"  {tag} PID={p['pid']:6d}  role={p['role']:10s}  "
            f"interpreter={p['python_label']}({os.path.basename(p['exe'])})  "
            f"cmd={p['cmdline'][:80]}"
        )
    return "\n".join(lines)


def _find_by_role(role: str) -> list[dict]:
    return [p for p in _discover_processes() if p["role"] == role]


def _kill_pid(pid: int, label: str) -> bool:
    """Kill a single PID. Returns True on success."""
    try:
        os.kill(pid, 9)
        logger.info("Killed %s PID=%d", label, pid)
        return True
    except ProcessLookupError:
        logger.info("%s PID=%d already dead", label, pid)
        return True
    except PermissionError:
        logger.error("Permission denied killing %s PID=%d", label, pid)
        return False
    except Exception as e:
        logger.error("Failed to kill %s PID=%d: %s", label, pid, e)
        return False


def kill_trader() -> bool:
    """Kill ONLY trader processes. Dashboard is never touched."""
    procs = _find_by_role("trader")
    if not procs:
        logger.info("No trader processes found.")
        return True
    ok = True
    for p in procs:
        logger.info(
            "Killing TRADER PID=%d  interpreter=%s  cmd=%s",
            p["pid"], p["python_label"], p["cmdline"][:60],
        )
        if not _kill_pid(p["pid"], "trader"):
            ok = False
    return ok


def kill_dashboard() -> bool:
    """Kill ONLY dashboard processes. Trader is never touched."""
    procs = _find_by_role("dashboard")
    if not procs:
        logger.info("No dashboard processes found.")
        return True
    ok = True
    for p in procs:
        logger.info(
            "Killing DASHBOARD PID=%d  interpreter=%s  cmd=%s",
            p["pid"], p["python_label"], p["cmdline"][:60],
        )
        if not _kill_pid(p["pid"], "dashboard"):
            ok = False
    return ok


def restart_trader() -> bool:
    """Kill trader, wait, relaunch. Dashboard is NEVER touched."""
    kill_trader()
    time.sleep(2)
    # Relaunch with system Python (matches how it was originally started)
    cmd = [sys.executable, "-u", "-m", "execution.mt5_trader"]
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    log_path = os.path.join(project_root, "logs", "trader_real.log")
    logger.info("Starting trader: %s  (logging to %s)", " ".join(cmd), log_path)
    with open(log_path, "a") as log_f:
        proc = subprocess.Popen(
            cmd, cwd=project_root, stdout=log_f, stderr=subprocess.STDOUT,
        )
    logger.info("Trader started PID=%d", proc.pid)
    # Save PID for Telegram admin compatibility
    pid_file = os.path.join(project_root, "logs", "trader.pid")
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    return True


def restart_dashboard() -> bool:
    """Kill dashboard, wait, relaunch. Trader is NEVER touched."""
    kill_dashboard()
    time.sleep(2)
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    venv_python = os.path.join(project_root, "venv", "Scripts", "python.exe")
    log_path = os.path.join(project_root, "logs", "dashboard.log")
    cmd = [venv_python, "-u", "-m", "uvicorn", "realtime.app:app",
           "--host", "127.0.0.1", "--port", "8000"]
    logger.info("Starting dashboard: %s", " ".join(cmd))
    with open(log_path, "a") as log_f:
        proc = subprocess.Popen(
            cmd, cwd=project_root, stdout=log_f, stderr=subprocess.STDOUT,
        )
    logger.info("Dashboard started PID=%d", proc.pid)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Safe process manager for trader/dashboard.")
    parser.add_argument("action", choices=[
        "status", "kill-trader", "kill-dashboard",
        "restart-trader", "restart-dashboard",
    ])
    args = parser.parse_args(argv)

    if args.action == "status":
        print(get_status())
    elif args.action == "kill-trader":
        kill_trader()
    elif args.action == "kill-dashboard":
        kill_dashboard()
    elif args.action == "restart-trader":
        restart_trader()
    elif args.action == "restart-dashboard":
        restart_dashboard()


if __name__ == "__main__":
    main()
