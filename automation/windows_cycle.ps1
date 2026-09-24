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

try {
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        throw "venv python not found: $Python"
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

    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] cycle start")
    & $Python "scheduler.py" "--tick" *>> $LogFile
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] cycle end")
}
catch {
    Add-Content -Path $LogFile -Value ("[" + (Get-Date) + "] ERROR: " + $_.Exception.Message)
    throw
}
finally {
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}
