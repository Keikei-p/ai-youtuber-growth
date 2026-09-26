$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("automation-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$mutex = New-Object System.Threading.Mutex($false, "AIYoutuberGrowthCycle")
if (-not $mutex.WaitOne(0)) {
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] another cycle is already running; skip")
    exit 0
}

# WakeToRunで起きた直後にWindowsが再スリープしないよう、
# このサイクルの実行中だけ画面を点けずにシステム起動を維持する。
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

try {
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        throw "venv python not found: $Python"
    }

    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] wake/cycle start")

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
        exit 0
    }

    # MIRAI_SAFE_SELF_UPDATE: mainの更新は別worktreeで検証後だけ反映。
    if (Test-Path (Join-Path $RepoRoot "safe_self_update.py")) {
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] safe self-update check")
        & $Python "safe_self_update.py" *>> $LogFile
        Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] safe self-update check end")
    }

    # MIRAI_DUE_FIRST: スリープ復帰後は重いAIサービスより投稿を最優先。
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] due-first start")
    & $Python "scheduler.py" "--run-due" *>> $LogFile
    $DueExitCode = $LASTEXITCODE
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] due-first end exit=" + $DueExitCode)
    if ($DueExitCode -ne 0) {
        throw "scheduler.py --run-due failed with exit code $DueExitCode"
    }

    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null
    }
    catch {
        $ollama = Get-Command ollama -ErrorAction SilentlyContinue
        if ($ollama) {
            Start-Process -FilePath $ollama.Source -ArgumentList "serve" -WindowStyle Hidden
            Start-Sleep -Seconds 3
        }
    }

    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:50021/version" -TimeoutSec 2 | Out-Null
    }
    catch {
        $envFile = Join-Path $RepoRoot ".env"
        if (Test-Path $envFile) {
            $line = Get-Content $envFile | Where-Object { $_ -match "^VOICEVOX_EXE=" } | Select-Object -First 1
            if ($line) {
                $voicevoxExe = ($line -replace "^VOICEVOX_EXE=", "").Trim().Trim('"')
                if ($voicevoxExe -and (Test-Path $voicevoxExe)) {
                    Start-Process -FilePath $voicevoxExe
                    Start-Sleep -Seconds 8
                }
            }
        }
    }

    & $Python "scheduler.py" "--tick" *>> $LogFile
    $ExitCode = $LASTEXITCODE
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] cycle end exit=" + $ExitCode)
    if ($ExitCode -ne 0) {
        throw "scheduler.py --tick failed with exit code $ExitCode"
    }
}
catch {
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] ERROR: " + $_.Exception.Message)
    try {
        if ($Python -and (Test-Path (Join-Path $RepoRoot "safe_code_repair.py"))) {
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
    [MiraiPowerState]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}
