$ErrorActionPreference = "SilentlyContinue"

$TaskName = "AI YouTuber Growth"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $task) {
    Write-Output "未登録"
    exit 0
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
$wake = $task.Settings.WakeToRun
$state = $task.State
$next = $info.NextRunTime

Write-Output ("登録済み / WakeToRun=" + $wake + " / State=" + $state + " / Next=" + $next)
