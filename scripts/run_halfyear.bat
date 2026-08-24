@echo off
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
if not exist "logs" mkdir "logs"
echo [%date% %time%] start half-year backtest >> "logs\halfyear.log" 2>&1
set "PYTHON=python"
if exist "venv\Scripts\python.exe" set "PYTHON=venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
if not exist "scripts\fetch_halfyear.py" (
  echo Missing scripts\fetch_halfyear.py >> "logs\halfyear.log"
  exit /b 1
)
"%PYTHON%" "scripts\fetch_halfyear.py" >> "logs\halfyear.log" 2>&1
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" "scripts\compare_halfyear.py" >> "logs\halfyear.log" 2>&1
echo [%date% %time%] done >> "logs\halfyear.log" 2>&1
exit /b %errorlevel%
