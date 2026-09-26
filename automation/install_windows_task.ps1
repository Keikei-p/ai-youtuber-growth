param(
    [int]$IntervalMinutes = 60,
    [int]$PrepareMinutes = 90,
    [int]$RecoveryMinutes = 15
)

$ErrorActionPreference = "Stop"

if ($IntervalMinutes -lt 15) {
    throw "IntervalMinutes must be 15 or greater."
}
if ($PrepareMinutes -lt 15) {
    throw "PrepareMinutes must be 15 or greater."
}
if ($RecoveryMinutes -lt 5) {
    throw "RecoveryMinutes must be 5 or greater."
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CycleScript = Join-Path $RepoRoot "automation\windows_cycle.ps1"
$TaskName = "AI YouTuber Growth"

if (-not (Test-Path $CycleScript)) {
    throw "Cycle script not found: $CycleScript"
}

$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + $CycleScript + '"') -WorkingDirectory $RepoRoot

# 管理画面で保存した投稿時刻を優先して読み取る。
$PostTimesRaw = ""
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $Python) {
    try {
        $PostTimesRaw = (& $Python -c "from runtime_control import post_times; print(post_times())" 2>$null | Select-Object -Last 1).Trim()
    }
    catch {
        $PostTimesRaw = ""
    }
}
if (-not $PostTimesRaw) {
    $EnvFile = Join-Path $RepoRoot ".env"
    if (Test-Path $EnvFile) {
        $Line = Get-Content $EnvFile | Where-Object { $_ -match "^POST_TIMES=" } | Select-Object -First 1
        if ($Line) {
            $PostTimesRaw = ($Line -replace "^POST_TIMES=", "").Trim().Trim('"')
        }
    }
}
if (-not $PostTimesRaw) {
    $PostTimesRaw = "09:00,15:00,21:00"
}

$TriggerMap = @{}

function Add-DailyWakeTrigger([DateTime]$When, [string]$Kind) {
    $Key = $When.ToString("HH:mm")
    if (-not $TriggerMap.ContainsKey($Key)) {
        $TriggerMap[$Key] = New-ScheduledTaskTrigger -Daily -At $When
    }
}

foreach ($Raw in ($PostTimesRaw -split ",")) {
    $Raw = $Raw.Trim()
    if (-not $Raw) { continue }
    try {
        $Parts = $Raw -split ":"
        $Hour = [int]$Parts[0]
        $Minute = [int]$Parts[1]
        $PostAt = (Get-Date).Date.AddHours($Hour).AddMinutes($Minute)
        Add-DailyWakeTrigger $PostAt "post"
        Add-DailyWakeTrigger $PostAt.AddMinutes(-$PrepareMinutes) "prepare"
        Add-DailyWakeTrigger $PostAt.AddMinutes($RecoveryMinutes) "recovery"
    }
    catch {
        Write-Warning "Invalid POST_TIMES entry ignored: $Raw"
    }
}

# 定期メンテナンスも残す。投稿時刻そのものは上の日次トリガーで保証する。
$MaintenanceStart = (Get-Date).AddMinutes(1)
$Maintenance = New-ScheduledTaskTrigger -Once -At $MaintenanceStart -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$Triggers = @($Maintenance) + @($TriggerMap.Values)

$Settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask -Action $Action -Trigger $Triggers -Settings $Settings -Principal $Principal
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

# AC/DCともWake Timerを有効化。デスクトップではAC設定が主に効く。
try {
    & powercfg.exe /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    & powercfg.exe /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    & powercfg.exe /SETACTIVE SCHEME_CURRENT | Out-Null
}
catch {
    Write-Warning "Wake timer power setting could not be changed automatically."
}

Write-Host ""
Write-Host "[OK] Windowsスリープ復帰自動運転タスクを登録しました。"
Write-Host "Task: $TaskName"
Write-Host "Maintenance: every $IntervalMinutes minutes"
Write-Host "Post times: $PostTimesRaw"
Write-Host "Prepare wake: $PrepareMinutes minutes before"
Write-Host "Recovery wake: $RecoveryMinutes minutes after"
Write-Host "WakeToRun: enabled"
Write-Host "StartWhenAvailable: enabled"
Write-Host ""
Write-Host "確認: powercfg /waketimers"
Write-Host "確認: Get-ScheduledTask -TaskName \"$TaskName\" | Select-Object -ExpandProperty Triggers"
Write-Host "すぐ1回テスト: schtasks /Run /TN \"$TaskName\""
