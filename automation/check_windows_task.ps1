param(
    [string]$TaskName = "AI YouTuber Growth"
)

$ErrorActionPreference = "Stop"

$Result = [ordered]@{
    platform = "windows"
    task_name = $TaskName
    registered = $false
    state = "missing"
    enabled = $false
    wake_to_run = $false
    start_when_available = $false
    triggers_count = 0
    last_run_time = $null
    next_run_time = $null
    last_task_result = $null
    missed_runs = $null
    wake_timer_present = $false
    wake_timer_text = ""
    checked_at = (Get-Date).ToString("o")
    error = ""
}

try {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop

    $Result.registered = $true
    $Result.state = [string]$Task.State
    $Result.enabled = [bool]$Task.Settings.Enabled
    $Result.wake_to_run = [bool]$Task.Settings.WakeToRun
    $Result.start_when_available = [bool]$Task.Settings.StartWhenAvailable
    $Result.triggers_count = @($Task.Triggers).Count
    $Result.last_run_time = (
        if ($Info.LastRunTime -and $Info.LastRunTime.Year -gt 1900) {
            $Info.LastRunTime.ToString("o")
        } else { $null }
    )
    $Result.next_run_time = (
        if ($Info.NextRunTime -and $Info.NextRunTime.Year -gt 1900) {
            $Info.NextRunTime.ToString("o")
        } else { $null }
    )
    $Result.last_task_result = [int64]$Info.LastTaskResult
    $Result.missed_runs = [int]$Info.NumberOfMissedRuns
}
catch {
    $Result.error = $_.Exception.Message
}

try {
    $WakeOutput = (& powercfg.exe /waketimers 2>&1 | Out-String).Trim()
    $Result.wake_timer_text = $WakeOutput
    if (
        $WakeOutput -and
        $WakeOutput -notmatch "There are no active wake timers" -and
        $WakeOutput -notmatch "アクティブなスリープ解除タイマーはありません"
    ) {
        $Result.wake_timer_present = $true
    }
}
catch {
    if (-not $Result.error) {
        $Result.error = "powercfg /waketimers: " + $_.Exception.Message
    }
}

$Result | ConvertTo-Json -Depth 5 -Compress
