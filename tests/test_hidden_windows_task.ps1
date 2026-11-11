$ErrorActionPreference = "Stop"

# Real Windows PowerShell 5.1 integration test against an inert, non-due
# disposable Task Scheduler task. Does not launch the Mirai production cycle.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $RepoRoot "automation\hidden_cycle_runner.vbs"
$Migrator = Join-Path $RepoRoot "automation\enable_hidden_windows_task.ps1"
$Installer = Join-Path $RepoRoot "automation\install_windows_task.ps1"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$CscriptExe = Join-Path $env:SystemRoot "System32\cscript.exe"

foreach ($Path in @($Runner, $Migrator, $Installer)) {
    if (-not (Test-Path $Path)) { throw "MISSING=$Path" }
}
[scriptblock]::Create((Get-Content -Raw -LiteralPath $Migrator)) | Out-Null
[scriptblock]::Create((Get-Content -Raw -LiteralPath $Installer)) | Out-Null

& $CscriptExe //B //Nologo $Runner --probe *> $null
if ($LASTEXITCODE -ne 17) {
    throw "INERT_HIDDEN_PROBE_FAILED=$LASTEXITCODE"
}
& $CscriptExe //B //Nologo $Runner --invalid-argument *> $null
if ($LASTEXITCODE -ne 64) {
    throw "UNEXPECTED_RUNNER_ARGUMENT_ALLOWED=$LASTEXITCODE"
}

& $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File $Installer -IntervalMinutes 60 -DryRun
if ($LASTEXITCODE -ne 0) { throw "INSTALLER_HIDDEN_DRY_RUN_FAILED" }

$Name = "AIYT Hidden Runner CI " + [guid]::NewGuid().ToString("N").Substring(0, 12)
$CycleScript = Join-Path $RepoRoot "automation\windows_cycle.ps1"
$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + $CycleScript + '"') -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddDays(2)
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal
$Registered = $false
try {
    Register-ScheduledTask -TaskName $Name -InputObject $Task -ErrorAction Stop | Out-Null
    $Registered = $true
    $OriginalXml = Export-ScheduledTask -TaskName $Name
    & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File $Migrator -RepoRoot $RepoRoot -TaskName $Name -DryRun
    if ($LASTEXITCODE -ne 0) { throw "MIGRATOR_DRY_RUN_FAILED" }
    & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File $Migrator -RepoRoot $RepoRoot -TaskName $Name
    if ($LASTEXITCODE -ne 0) { throw "MIGRATOR_APPLY_FAILED" }
    $Updated = Get-ScheduledTask -TaskName $Name
    if ($Updated.Actions[0].Execute -inotmatch 'wscript.exe$') {
        throw "HIDDEN_ACTION_MISSING"
    }
    [xml]$Before = $OriginalXml
    [xml]$After = Export-ScheduledTask -TaskName $Name
    foreach ($Section in @("Triggers", "Principals", "Settings")) {
        $X = $Before.DocumentElement.SelectSingleNode("*[local-name()='$Section']")
        $Y = $After.DocumentElement.SelectSingleNode("*[local-name()='$Section']")
        if ($null -eq $X -or $null -eq $Y -or $X.InnerXml -cne $Y.InnerXml) {
            throw "MIGRATION_MODIFIED=$Section"
        }
    }
    Write-Host "WINDOWS_INVISIBLE_TASK_MIGRATION=PASS"
}
finally {
    if ($Registered) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    }
}
