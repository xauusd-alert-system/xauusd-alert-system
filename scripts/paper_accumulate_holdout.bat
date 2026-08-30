@echo off
rem Phase 4 holdout accumulation: one paper_accumulate tick per invocation.
rem Windows Task Scheduler runs this every 15 min (M15 candle cadence).
cd /d C:\Users\botbo\Desktop\xauusd-alert-system
C:\Users\botbo\Desktop\xauusd-alert-system\venv\Scripts\python.exe -m scripts.paper_accumulate run --manifest config\paper_manifest_xauusd_subsetext_holdout.json --n-candles 2200 --once >> logs\paper_accumulate_subsetext.log 2>&1
