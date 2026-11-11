param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "AI YouTuber Growth",
    [string]$ExpectedMainSha = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path

# Never run scheduler.py directly or alter SQLite, media, OAuth, or queues.
# Change only the existing Task Scheduler action; retain triggers/principal/settings.
$CycleScript = Join-Path $RepoRoot "automation\windows_cycle.ps1"
$RunnerPath = Join-Path $RepoRoot "automation\hidden_cycle_runner.vbs"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$CscriptExe = Join-Path $env:SystemRoot "System32\cscript.exe"
$WscriptExe = Join-Path $env:SystemRoot "System32\wscript.exe"

foreach ($Required in @($CycleScript, $PowerShellExe, $CscriptExe, $WscriptExe)) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
        throw "REQUIRED_FILE_MISSING=$Required"
    }
}

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if (@($Task).Count -ne 1 -or $Task.TaskPath -ne "\") {
    throw "TASK_AMBIGUOUS_OR_NOT_IN_ROOT"
}
if (@($Task.Actions).Count -ne 1) {
    throw "CUSTOM_ACTIONS_PRESENT_ABORT"
}

$OldAction = $Task.Actions[0]
$OldPowerShellArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $CycleScript + '"'
$NewRunnerArgs = '//B //Nologo "' + $RunnerPath + '"'
$HiddenAction = New-ScheduledTaskAction -Execute $WscriptExe -Argument $NewRunnerArgs -WorkingDirectory $RepoRoot

$AlreadyHidden = (
    $OldAction.Execute -ieq $HiddenAction.Execute -and
    $OldAction.Arguments -ceq $HiddenAction.Arguments -and
    $OldAction.WorkingDirectory -ieq $HiddenAction.WorkingDirectory
)
if (-not $AlreadyHidden) {
    $IsKnownOriginal = (
        $OldAction.Execute -ieq $PowerShellExe -or
        $OldAction.Execute -ieq "powershell.exe"
    )
    if (-not $IsKnownOriginal -or $OldAction.Arguments -cne $OldPowerShellArgs) {
        throw "UNRECOGNIZED_OR_MODIFIED_TASK_ACTION_ABORT"
    }
    if ($OldAction.WorkingDirectory -and $OldAction.WorkingDirectory -ine $RepoRoot) {
        throw "TASK_WORKING_DIRECTORY_MISMATCH"
    }
    if ([string]$Task.State -ne "Ready") {
        throw "TASK_NOT_IDLE=$($Task.State)"
    }
}

# The one-paste PC command pins origin/main; only the reviewed launcher is imported.
$LauncherContent = ""
if ($ExpectedMainSha) {
    $Remote = (& git -C $RepoRoot remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $Remote -notmatch 'github.com[:/]Keikei-p/ai-youtuber-growth(\.git)?$') {
        throw "WRONG_REPOSITORY"
    }
    $CurrentSha = (& git -C $RepoRoot rev-parse origin/main).Trim()
    if ($LASTEXITCODE -ne 0 -or $CurrentSha -ne $ExpectedMainSha) {
        throw "REMOTE_COMMIT_NOT_VERIFIED"
    }
    $LauncherLines = @(& git -C $RepoRoot show "origin/main:automation/hidden_cycle_runner.vbs")
    if ($LASTEXITCODE -ne 0 -or $LauncherLines.Count -lt 10) {
        throw "REVIEWED_LAUNCHER_UNAVAILABLE"
    }
    $LauncherContent = ($LauncherLines -join [char]10) + [char]10
}
elseif (Test-Path -LiteralPath $RunnerPath) {
    $LauncherContent = [System.IO.File]::ReadAllText($RunnerPath)
}
else {
    throw "LAUNCHER_UNAVAILABLE_WITHOUT_VERIFIED_MAIN"
}

# Check that the local launcher was not edited independently.
if (Test-Path -LiteralPath $RunnerPath) {
    $LocalContent = [System.IO.File]::ReadAllText($RunnerPath)
    if ($LocalContent.Replace([string][char]13, "").Trim() -ne
        $LauncherContent.Replace([string][char]13, "").Trim()) {
        throw "LOCAL_LAUNCHER_MODIFIED_ABORT"
    }
}

$ProbeFile = Join-Path $env:TEMP ("mirai-hidden-probe-" + [guid]::NewGuid().ToString("N") + ".vbs")
try {
    [System.IO.File]::WriteAllText($ProbeFile, $LauncherContent, [System.Text.Encoding]::ASCII)
    & $CscriptExe //B //Nologo $ProbeFile --probe *> $null
    if ($LASTEXITCODE -ne 17) {
        throw "WINDOWS_SCRIPT_HOST_PROBE_FAILED=$LASTEXITCODE"
    }
}
finally {
    Remove-Item -LiteralPath $ProbeFile -Force -ErrorAction SilentlyContinue
}

if ($DryRun) {
    Write-Host "HIDDEN_TASK_DRY_RUN=PASS"
    Write-Host "ORIGINAL_ACTION_RECOGNIZED=$(-not $AlreadyHidden)"
    Write-Host "TASK_STATE=$($Task.State)"
    exit 0
}
if ($AlreadyHidden) {
    Write-Host "HIDDEN_TASK_ACTION=ALREADY_ENABLED"
    Write-Host "TASK_STATE=$($Task.State)"
    exit 0
}

# Protect all non-action configuration with a dated XML backup. Do not
# disable/re-register the task; Set-ScheduledTask updates the action in place.
$BackupRoot = Join-Path $env:LOCALAPPDATA "YOROKOBI\MiraiProductionOS\task-backups"
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
$BackupPath = Join-Path $BackupRoot ("ai-youtuber-growth-before-hidden-" + (Get-Date -Format "yyyyMMdd-HHmmss-ffff") + ".xml")
if (Test-Path -LiteralPath $BackupPath) {
    throw "BACKUP_ALREADY_EXISTS"
}
$OriginalXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $Task.TaskPath -ErrorAction Stop
[System.IO.File]::WriteAllText($BackupPath, $OriginalXml, [System.Text.UTF8Encoding]::new($false))

$LauncherCreated = $false
if (-not (Test-Path -LiteralPath $RunnerPath)) {
    [System.IO.File]::WriteAllText($RunnerPath, $LauncherContent, [System.Text.Encoding]::ASCII)
    $LauncherCreated = $true
}
$ActionApplied = $false
try {
    Set-ScheduledTask -TaskName $TaskName -TaskPath $Task.TaskPath -Action $HiddenAction -ErrorAction Stop | Out-Null
    $ActionApplied = $true
    $Updated = Get-ScheduledTask -TaskName $TaskName -TaskPath $Task.TaskPath -ErrorAction Stop
    if (@($Updated.Actions).Count -ne 1 -or
        $Updated.Actions[0].Execute -ine $HiddenAction.Execute -or
        $Updated.Actions[0].Arguments -cne $HiddenAction.Arguments -or
        $Updated.Actions[0].WorkingDirectory -ine $HiddenAction.WorkingDirectory) {
        throw "TASK_ACTION_VERIFICATION_FAILED"
    }
    [xml]$BeforeXml = $OriginalXml
    [xml]$AfterXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $Task.TaskPath -ErrorAction Stop
    foreach ($Section in @("Triggers", "Principals", "Settings")) {
        $BeforeNode = $BeforeXml.DocumentElement.SelectSingleNode("*[local-name()='$Section']")
        $AfterNode = $AfterXml.DocumentElement.SelectSingleNode("*[local-name()='$Section']")
        if ($null -eq $BeforeNode -or $null -eq $AfterNode -or
            $BeforeNode.InnerXml -cne $AfterNode.InnerXml) {
            throw "TASK_CONFIG_CHANGED=$Section"
        }
    }
    Write-Host "HIDDEN_TASK_ACTION=ENABLED"
    Write-Host "TASK_BACKUP=$BackupPath"
    Write-Host "PRESERVED_TRIGGERS=$(@($Updated.Triggers).Count)"
    Write-Host "TASK_STATE=$($Updated.State)"
    Write-Host "TASK_NEXT_RUN=$((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"
}
catch {
    if ($ActionApplied) {
        try {
            Set-ScheduledTask -TaskName $TaskName -TaskPath $Task.TaskPath -Action $OldAction -ErrorAction Stop | Out-Null
            Write-Host "ROLLBACK_ACTION=RESTORED"
        }
        catch {
            Write-Warning "ROLLBACK_ACTION_FAILED: review backup $BackupPath"
        }
    }
    throw
}
