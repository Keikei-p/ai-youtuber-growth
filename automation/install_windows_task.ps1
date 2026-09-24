param(
    [int]$IntervalMinutes = 60
)

$ErrorActionPreference = "Stop"

if ($IntervalMinutes -lt 15) {
    throw "IntervalMinutes must be 15 or greater."
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CycleScript = Join-Path $RepoRoot "automation\windows_cycle.ps1"
$TaskName = "AI YouTuber Growth"

if (-not (Test-Path $CycleScript)) {
    throw "Cycle script not found: $CycleScript"
}

$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + $CycleScript + '"') -WorkingDirectory $RepoRoot
$StartAt = (Get-Date).AddMinutes(1)
$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

# AC電源時のWake Timerを有効化。権限等で失敗してもタスク登録自体は維持。
try {
    & powercfg.exe /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    & powercfg.exe /SETACTIVE SCHEME_CURRENT | Out-Null
}
catch {
    Write-Warning "Wake timer power setting could not be changed automatically."
}

Write-Host ""
Write-Host "[OK] Windows省負荷自動運転タスクを登録しました。"
Write-Host "Task: $TaskName"
Write-Host "Interval: $IntervalMinutes minutes"
Write-Host "WakeToRun: enabled"
Write-Host "StartWhenAvailable: enabled"
Write-Host ""
Write-Host "確認: powercfg /waketimers"
Write-Host "すぐ1回テスト: schtasks /Run /TN \"AI YouTuber Growth\""
