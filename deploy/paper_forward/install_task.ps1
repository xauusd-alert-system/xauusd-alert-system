param(
    [string]$TaskName = "XAUUSD Frozen Paper Accumulator"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner = Join-Path $PSScriptRoot "run_paper_accumulator.bat"
if (-not (Test-Path $Runner)) { throw "Runner missing: $Runner" }

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$Runner`"`"" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Frozen XAUUSD paper/shadow accumulator; no broker order routing" `
    -Force

Write-Host "Installed task: $TaskName"
Write-Host "Set PAPER_MANIFEST_PATH and PAPER_LEDGER_DB as system/user environment variables before starting it."
