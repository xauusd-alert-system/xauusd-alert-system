@echo off
REM Start the MT5 trader watchdog in background
REM Usage: watchdog_start.bat

cd /d "%~dp0\.."

echo Starting watchdog...
start /b pythonw scripts/watchdog.py

echo Watchdog started. Check logs/watchdog.log for status.
echo Health check: type "type logs\watchdog_heartbeat.json"
