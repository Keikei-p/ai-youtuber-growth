param(
    [int]$IntervalMinutes = 10
)

$ErrorActionPreference = "Stop"

if ($IntervalMinutes -lt 5) {
    throw "IntervalMinutes must be 5 or greater."
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CycleScript = Join-Path $RepoRoot "automation\windows_cycle.ps1"
$TaskName = "AI YouTuber Growth"

if (-not (Test-Path $CycleScript)) {
    throw "Cycle script not found: $CycleScript"
}

$PowerShell = (Get-Command powershell.exe).Source
$TaskCommand = '"' + $PowerShell + '" -NoProfile -ExecutionPolicy Bypass -File "' + $CycleScript + '"'

& schtasks.exe /Create /F /TN $TaskName /TR $TaskCommand /SC MINUTE /MO $IntervalMinutes | Out-Host

Write-Host ""
Write-Host "[OK] Windows自動化タスクを登録しました。"
Write-Host "Task: $TaskName"
Write-Host "Interval: $IntervalMinutes minutes"
Write-Host ""
Write-Host "すぐ1回テストする場合:"
Write-Host "schtasks /Run /TN `"AI YouTuber Growth`""
