param(
    [int]$IntervalMinutes = 15,
    [int]$PrepareMinutes = 90,
    [int[]]$RecoveryMinutes = @(5, 15, 30, 60),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($IntervalMinutes -lt 15) {
    throw "IntervalMinutes must be 15 or greater."
}
if ($PrepareMinutes -lt 15) {
    throw "PrepareMinutes must be 15 or greater."
}
foreach ($RecoveryMinute in $RecoveryMinutes) {
    if ($RecoveryMinute -lt 5) {
        throw "RecoveryMinutes entries must be 5 or greater."
    }
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CycleScript = Join-Path $RepoRoot "automation\windows_cycle.ps1"
$TaskName = "AI YouTuber Growth"

if (-not (Test-Path $CycleScript)) {
    throw "Cycle script not found: $CycleScript"
}

$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + $CycleScript + '"') -WorkingDirectory $RepoRoot

# Prefer the post times saved in the dashboard.
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
        # MIRAI_RECOVERY_TRIGGERS: retry after missed wakes or delayed network recovery.
        foreach ($RecoveryMinute in $RecoveryMinutes) {
            Add-DailyWakeTrigger $PostAt.AddMinutes($RecoveryMinute) ("recovery-" + $RecoveryMinute)
        }
    }
    catch {
        Write-Warning "Invalid POST_TIMES entry ignored: $Raw"
    }
}

# Keep periodic maintenance; daily triggers handle the scheduled post times.
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

if ($DryRun) {
    if (-not $Task.Settings.WakeToRun) {
        throw "DryRun: WakeToRun was not preserved."
    }
    if (@($Task.Triggers).Count -lt 4) {
        throw "DryRun: expected maintenance + daily wake triggers."
    }
    Write-Host (
        "[DRY-RUN] task definition OK / triggers=" +
        @($Task.Triggers).Count +
        " / post_times=" + $PostTimesRaw
    )
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

# Verify the task from Windows after registration; do not trust API success alone.
$RegisteredTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$RegisteredInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
if (-not $RegisteredTask.Settings.WakeToRun) {
    throw "Registered task verification failed: WakeToRun=false"
}
if (-not $RegisteredTask.Settings.StartWhenAvailable) {
    throw "Registered task verification failed: StartWhenAvailable=false"
}
if ([string]$RegisteredTask.State -eq "Disabled") {
    throw "Registered task verification failed: task disabled"
}
if (@($RegisteredTask.Triggers).Count -lt 4) {
    throw "Registered task verification failed: too few triggers"
}

# Enable wake timers for both AC and DC power (AC is most relevant for desktops).
try {
    & powercfg.exe /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    & powercfg.exe /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    & powercfg.exe /SETACTIVE SCHEME_CURRENT | Out-Null
}
catch {
    Write-Warning "Wake timer power setting could not be changed automatically."
}

Write-Host ""
Write-Host "[OK] Windows wake task for autopilot registered and verified."
Write-Host "Task: $TaskName"
Write-Host "Maintenance: every $IntervalMinutes minutes"
Write-Host "Post times: $PostTimesRaw"
Write-Host "Prepare wake: $PrepareMinutes minutes before"
Write-Host "Recovery wake: $($RecoveryMinutes -join ',') minutes after"
Write-Host "WakeToRun: enabled"
Write-Host "StartWhenAvailable: enabled"
Write-Host "Registered triggers: $(@($RegisteredTask.Triggers).Count)"
Write-Host "Last run: $($RegisteredInfo.LastRunTime)"
Write-Host "Next run: $($RegisteredInfo.NextRunTime)"
Write-Host "Last task result: $($RegisteredInfo.LastTaskResult)"
Write-Host ""
Write-Host "Check wake timers: powercfg /waketimers"
Write-Host ('Check triggers: Get-ScheduledTask -TaskName "{0}" | Select-Object -ExpandProperty Triggers' -f $TaskName)
Write-Host ('Run one test now: schtasks /Run /TN "{0}"' -f $TaskName)
