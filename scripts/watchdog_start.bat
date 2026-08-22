@echo off
REM Start the MT5 trader watchdog in background
REM Usage: watchdog_start.bat
REM Uses the venv python so the spawned trader child (sys.executable) has all deps.

cd /d "%~dp0\.."

echo Starting watchdog...
start /b "" "%~dp0..\venv\Scripts\pythonw.exe" "%~dp0watchdog.py"

echo Watchdog started. Check logs/watchdog.log for status.
echo Health check: type "type logs\watchdog_heartbeat.json"
