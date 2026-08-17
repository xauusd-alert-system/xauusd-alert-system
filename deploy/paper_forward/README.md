# Windows frozen paper accumulator

This task runs the paper/shadow process only. It never imports the MT5 trader's
order-routing loop and cannot submit broker orders.

1. Create the frozen manifest once (see `docs/CANDIDATE_WIDE_TREND_FILTERED.md`).
2. Set user/system environment variables:
   - `PAPER_MANIFEST_PATH`
   - `PAPER_LEDGER_DB`
3. In an elevated PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\paper_forward\install_task.ps1
Start-ScheduledTask -TaskName "XAUUSD Frozen Paper Accumulator"
```

The task starts at Windows boot and restarts after failure. Logs are appended to
`logs\paper_accumulator.log`. Check only liveness/sample counts during accumulation:

```powershell
.venv\Scripts\python.exe -m scripts.paper_accumulate status `
  --manifest $env:PAPER_MANIFEST_PATH --db-path $env:PAPER_LEDGER_DB
```

Do not run `scripts.run_live_forward_validation` before the pre-registered minimum.
