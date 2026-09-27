param(
    [switch]$Json
)

$ErrorActionPreference = "SilentlyContinue"

$TaskName = "AI YouTuber Growth"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

$resultObject = [ordered]@{
    platform = "windows"
    task_name = $TaskName
    registered = $false
    state = "missing"
    enabled = $false
    wake_to_run = $false
    start_when_available = $false
    triggers_count = 0
    trigger_times = @()
    last_run_time = $null
    next_run_time = $null
    last_task_result = $null
    missed_runs = $null
    wake_timer_present = $false
    wake_timer_text = ""
    checked_at = (Get-Date).ToString("o")
    error = ""
}

if ($task) {
    try {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
        $triggerTimes = @(
            $task.Triggers |
            ForEach-Object {
                if ($_.StartBoundary) {
                    try {
                        ([datetime]$_.StartBoundary).ToString("HH:mm")
                    }
                    catch {
                        [string]$_.StartBoundary
                    }
                }
            } |
            Sort-Object -Unique
        )

        $resultObject.registered = $true
        $resultObject.state = [string]$task.State
        $resultObject.enabled = [bool]$task.Settings.Enabled
        $resultObject.wake_to_run = [bool]$task.Settings.WakeToRun
        $resultObject.start_when_available = [bool]$task.Settings.StartWhenAvailable
        $resultObject.triggers_count = @($task.Triggers).Count
        $resultObject.trigger_times = $triggerTimes
        $resultObject.last_run_time = (
            if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1900) {
                $info.LastRunTime.ToString("o")
            }
            else { $null }
        )
        $resultObject.next_run_time = (
            if ($info.NextRunTime -and $info.NextRunTime.Year -gt 1900) {
                $info.NextRunTime.ToString("o")
            }
            else { $null }
        )
        $resultObject.last_task_result = [int64]$info.LastTaskResult
        $resultObject.missed_runs = [int]$info.NumberOfMissedRuns
    }
    catch {
        $resultObject.error = $_.Exception.Message
    }
}

try {
    $wakeOutput = (& powercfg.exe /waketimers 2>&1 | Out-String).Trim()
    $resultObject.wake_timer_text = $wakeOutput
    if (
        $wakeOutput -and
        $wakeOutput -notmatch "There are no active wake timers" -and
        $wakeOutput -notmatch "アクティブなスリープ解除タイマーはありません"
    ) {
        $resultObject.wake_timer_present = $true
    }
}
catch {
    if (-not $resultObject.error) {
        $resultObject.error = "powercfg /waketimers: " + $_.Exception.Message
    }
}

if ($Json) {
    $resultObject | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

if (-not $resultObject.registered) {
    Write-Output "未登録"
    exit 0
}

Write-Output (
    "登録済み / WakeToRun=" + $resultObject.wake_to_run +
    " / StartWhenAvailable=" + $resultObject.start_when_available +
    " / State=" + $resultObject.state +
    " / Next=" + $resultObject.next_run_time +
    " / Last=" + $resultObject.last_run_time +
    " / LastResult=" + $resultObject.last_task_result +
    " / Triggers=" + ($resultObject.trigger_times -join ",") +
    " / WakeTimer=" + $resultObject.wake_timer_present
)
