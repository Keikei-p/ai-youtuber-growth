$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("automation-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$StatusFile = Join-Path $RepoRoot "automation\last_cycle_status.json"
$FailureStateFile = Join-Path $RepoRoot "automation\cycle_failure_state.json"
$RunStartedAt = (Get-Date).ToString("o")
$RunId = [guid]::NewGuid().ToString("N")
$LastExitCode = $null
$CurrentStage = "initialize"

function Read-FailureState {
    if (Test-Path $FailureStateFile) {
        try {
            $state = Get-Content -Raw -Path $FailureStateFile | ConvertFrom-Json
            return @{ failures = [int]$state.consecutive_failures; cooldown_until = [string]$state.cooldown_until }
        }
        catch {
            Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] WARN invalid cycle_failure_state.json: " + $_.Exception.Message)
        }
    }
    return @{ failures = 0; cooldown_until = "" }
}

function Save-FailureState {
    param([int]$Failures, [string]$CooldownUntil = "")
    $payload = [ordered]@{
        consecutive_failures = $Failures
        cooldown_until = $CooldownUntil
        updated_at = (Get-Date).ToString("o")
    }
    $temp = $FailureStateFile + "." + $PID + ".tmp"
    $payload | ConvertTo-Json | Set-Content -Path $temp -Encoding UTF8
    Move-Item -Force -Path $temp -Destination $FailureStateFile
}

function Next-TaskRun {
    try {
        $info = Get-ScheduledTaskInfo -TaskName "AI YouTuber Growth" -ErrorAction Stop
        if ($info.NextRunTime.Year -gt 1900) {
            return $info.NextRunTime.ToString("o")
        }
    }
    catch {}
    return ""
}

function Write-CycleStatus {
    param(
        [string]$Status,
        [string]$Stage,
        [string]$Detail = "",
        [hashtable]$Extra = @{}
    )

    $script:CurrentStage = $Stage
    $Payload = [ordered]@{
        status = $Status
        stage = $Stage
        detail = $Detail
        run_id = $RunId
        started_at = $RunStartedAt
        finished_at = $(if ($Status -in @("success", "failed", "retry_wait", "cooldown", "skipped")) { (Get-Date).ToString("o") } else { $null })
        exit_code = $LastExitCode
        consecutive_failures = (Read-FailureState).failures
        next_run_at = Next-TaskRun
        updated_at = (Get-Date).ToString("o")
        pid = $PID
        log_file = $LogFile
    }
    foreach ($Key in $Extra.Keys) {
        $Payload[$Key] = $Extra[$Key]
    }

    $Temp = $StatusFile + "." + $PID + ".tmp"
    $Payload | ConvertTo-Json -Depth 6 | Set-Content -Path $Temp -Encoding UTF8
    Move-Item -Force -Path $Temp -Destination $StatusFile
}


function Resolve-MiraiPython {
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        return $VenvPython
    }

    $Py = Get-Command py.exe -ErrorAction SilentlyContinue
    $PythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $Py -and -not $PythonCmd) {
        throw "Python runtime not found. .venv / py.exe / python.exe are all unavailable."
    }

    Write-CycleStatus -Status "running" -Stage "python_repair" -Detail ".venvが無いため自動作成しています。"
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] .venv missing; creating automatically")

    if ($Py) {
        & $Py.Source -3 -m venv (Join-Path $RepoRoot ".venv") *>> $LogFile
    }
    else {
        & $PythonCmd.Source -m venv (Join-Path $RepoRoot ".venv") *>> $LogFile
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        throw "Failed to create .venv Python runtime."
    }
    return $VenvPython
}

function Ensure-MiraiDependencies {
    param([string]$PythonPath)

    & $PythonPath -c "import dotenv, requests" *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-CycleStatus -Status "running" -Stage "dependency_repair" -Detail "Python依存関係を自動修復しています。"
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] Python dependencies missing; repairing")
    & $PythonPath -m pip install -r (Join-Path $RepoRoot "requirements.txt") *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        throw "pip install -r requirements.txt failed."
    }
}

$mutex = New-Object System.Threading.Mutex($false, "AIYoutuberGrowthCycle")
if (-not $mutex.WaitOne(0)) {
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] another cycle is already running; skip")
    $LastExitCode = 0
    Write-CycleStatus -Status "skipped" -Stage "mutex_busy" -Detail "A previous cycle is still running; no duplicate process started."
    $mutex.Dispose()
    exit 0
}

# Keep the machine awake during a cycle, without activating the display.
try {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class MiraiPowerState {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
    $ES_CONTINUOUS = [uint32]0x80000000
    $ES_SYSTEM_REQUIRED = [uint32]0x00000001
    [MiraiPowerState]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null

    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] cycle start run_id=" + $RunId)
    $failureState = Read-FailureState
    if ($failureState.cooldown_until) {
        $until = [datetimeoffset]::MinValue
        if ([datetimeoffset]::TryParse($failureState.cooldown_until, [ref]$until) -and (Get-Date) -lt $until.LocalDateTime) {
            Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] cooldown: hold heavy tasks until " + $failureState.cooldown_until)
            $LastExitCode = 0
            Write-CycleStatus -Status "cooldown" -Stage "consecutive_failure_hold" -Detail ("Safety cooldown until " + $failureState.cooldown_until)
            exit 0
        }
    }
    $Python = Resolve-MiraiPython
    Ensure-MiraiDependencies -PythonPath $Python

    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] wake/cycle start")
    Write-CycleStatus -Status "running" -Stage "wake_start" -Detail "Windowsタスクが起動しました。"

    # スリープ復帰直後はNIC/DNSが戻るまで時間がかかることがある。
    # 最大90秒待ち、戻らなければキューを消費せず次の回復トリガーへ回す。
    $NetworkReady = $false
    for ($i = 0; $i -lt 18; $i++) {
        try {
            if (Test-NetConnection -ComputerName "www.googleapis.com" -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue) {
                $NetworkReady = $true
                break
            }
        }
        catch {}
        Start-Sleep -Seconds 5
    }
    if (-not $NetworkReady) {
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] network not ready after wake; keep queue and retry later")
        $LastExitCode = 0
        Write-CycleStatus -Status "retry_wait" -Stage "network_wait" -Detail "スリープ復帰後90秒以内にGoogle APIへのHTTPS接続を確認できませんでした。キューは保持し、次の回復トリガーで再試行します。"
        exit 0
    }

    Write-CycleStatus -Status "running" -Stage "network_ready" -Detail "ネットワーク復旧を確認しました。"

    # MIRAI_DUE_FIRST: スリープ復帰後は重いAIサービスより投稿を最優先。
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] due-first start")
    Write-CycleStatus -Status "running" -Stage "due_first" -Detail "AIサービス起動より先にYouTube投稿判定を実行しています。"
    & $Python "scheduler.py" "--run-due" *>> $LogFile
    $DueExitCode = $LASTEXITCODE
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] due-first end exit=" + $DueExitCode)
    if ($DueExitCode -eq 0) {
        Write-CycleStatus -Status "running" -Stage "due_first_done" -Detail "期限投稿判定が終了しました。" -Extra @{ due_exit_code = $DueExitCode }
    }
    if ($DueExitCode -ne 0) {
        throw "scheduler.py --run-due failed with exit code $DueExitCode"
    }

    # MIRAI_SAFE_SELF_UPDATE: 投稿判定を先に終えた後でだけ自己更新を確認。
    if (Test-Path (Join-Path $RepoRoot "safe_self_update.py")) {
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] safe self-update check")
        Write-CycleStatus -Status "running" -Stage "self_update" -Detail "投稿判定後に安全な自己更新を確認しています。"
        & $Python "safe_self_update.py" *>> $LogFile
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] safe self-update check end")
    }

    # 自己更新後に依存関係が増えた場合もその場で修復。
    Ensure-MiraiDependencies -PythonPath $Python

    # Ollama / VOICEVOX / FFmpegをPython側で統一診断・自動起動。
    if (Test-Path (Join-Path $RepoRoot "runtime_bootstrap.py")) {
        Write-CycleStatus -Status "running" -Stage "runtime_bootstrap" -Detail "AIサービスを自動点検・復旧しています。"
        & $Python "runtime_bootstrap.py" *>> $LogFile
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] runtime bootstrap exit=" + $LASTEXITCODE)
    }

    Write-CycleStatus -Status "running" -Stage "tick" -Detail "通常の生成・分析サイクルを実行しています。"
    & $Python "scheduler.py" "--tick" *>> $LogFile
    $ExitCode = $LASTEXITCODE
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] cycle end exit=" + $ExitCode)
    if ($ExitCode -eq 0) {
        $LastExitCode = 0
        Save-FailureState -Failures 0
        Write-CycleStatus -Status "success" -Stage "complete" -Detail "スリープ復帰/自動投稿サイクルを正常終了しました。" -Extra @{ tick_exit_code = $ExitCode; due_exit_code = $DueExitCode }
    }
    if ($ExitCode -ne 0) {
        throw "scheduler.py --tick failed with exit code $ExitCode"
    }
}
catch {
    $FailureStage = $CurrentStage
    $FailureDetail = $_.Exception.Message
    $LastExitCode = 1
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] ERROR stage=" + $FailureStage + " message=" + $FailureDetail)
    $failureState = Read-FailureState
    $Failures = [int]$failureState.failures + 1
    $CooldownUntil = ""
    if ($Failures -ge 3) {
        $CooldownUntil = (Get-Date).AddMinutes(30).ToString("o")
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] SAFETY: 3+ failures, cooldown until " + $CooldownUntil)
    }
    try { Save-FailureState -Failures $Failures -CooldownUntil $CooldownUntil }
    catch { Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] WARN could not save failure count: " + $_.Exception.Message) }
    try {
        Write-CycleStatus -Status "failed" -Stage $FailureStage -Detail $FailureDetail -Extra @{ failure_count = $Failures; cooldown_until = $CooldownUntil }
    }
    catch {}
    try {
        if ($Failures -lt 3 -and $Python -and (Test-Path (Join-Path $RepoRoot "safe_code_repair.py"))) {
            Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] guarded code repair check start")
            & $Python "safe_code_repair.py" *>> $LogFile
            Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] guarded code repair check end")
        }
    }
    catch {
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] guarded repair failed: " + $_.Exception.Message)
    }
    throw
}
finally {
    try {
        [MiraiPowerState]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    }
    catch {}
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}
