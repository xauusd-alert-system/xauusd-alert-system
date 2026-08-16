@echo off
setlocal
cd /d "%~dp0\..\.."

if "%PAPER_MANIFEST_PATH%"=="" set "PAPER_MANIFEST_PATH=config\paper\xauusd_wide_trend_filtered.json"
if "%PAPER_LEDGER_DB%"=="" set "PAPER_LEDGER_DB=data\paper_forward.sqlite"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found. Install the pinned environment first.
  exit /b 2
)
if not exist "%PAPER_MANIFEST_PATH%" (
  echo ERROR: frozen manifest not found: %PAPER_MANIFEST_PATH%
  exit /b 3
)

if not exist "logs" mkdir "logs"
.venv\Scripts\python.exe -m scripts.paper_accumulate run ^
  --manifest "%PAPER_MANIFEST_PATH%" ^
  --db-path "%PAPER_LEDGER_DB%" >> "logs\paper_accumulator.log" 2>&1
exit /b %ERRORLEVEL%
