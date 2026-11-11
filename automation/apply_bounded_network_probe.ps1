param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedMainSha
)
$ErrorActionPreference = "Stop"

# Apply only the known network probe block, preserving unrelated local changes.
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$Task = Get-ScheduledTask -TaskName "AI YouTuber Growth" -ErrorAction Stop
if ([string]$Task.State -ne "Disabled") { throw "STOP_SCHEDULER_FIRST" }
$Service = New-Object -ComObject Schedule.Service
$Service.Connect()
if ($Service.GetFolder($Task.TaskPath).GetTask($Task.TaskName).GetInstances(0).Count -gt 0) {
    throw "SCHEDULER_PROCESS_STILL_RUNNING"
}
$Current = (& git -C $RepoRoot rev-parse origin/main).Trim()
if ($LASTEXITCODE -ne 0 -or $Current -ne $ExpectedMainSha) {
    throw "REMOTE_COMMIT_NOT_VERIFIED"
}
$Remote = (& git -C $RepoRoot remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or $Remote -notmatch 'github.com[:/]Keikei-p/ai-youtuber-growth(\.git)?$') {
    throw "WRONG_REPOSITORY"
}
$Helper = Join-Path $RepoRoot "automation\network_probe.ps1"
$Cycle = Join-Path $RepoRoot "automation\windows_cycle.ps1"
if (-not (Test-Path -LiteralPath $Cycle)) { throw "WINDOWS_CYCLE_NOT_FOUND" }

# Helper content is ASCII, so Windows PowerShell's Git encoding is safe.
$RemoteLines = @(& git -C $RepoRoot show "origin/main:automation/network_probe.ps1")
if ($LASTEXITCODE -ne 0 -or $RemoteLines.Count -lt 10) {
    throw "REMOTE_HELPER_NOT_FOUND"
}
$LF = [string][char]10
$CRLF = [string][char]13 + [string][char]10
$HelperText = ($RemoteLines -join $LF) + $LF
[scriptblock]::Create($HelperText) | Out-Null
if (Test-Path -LiteralPath $Helper) {
    $LocalHelper = [System.IO.File]::ReadAllText($Helper)
    if ($LocalHelper.Replace($CRLF,$LF).Trim() -ne $HelperText.Trim()) {
        throw "LOCAL_HELPER_MODIFIED_ABORT"
    }
}

$OriginalBytes = [System.IO.File]::ReadAllBytes($Cycle)
$KeepBom = ($OriginalBytes.Length -ge 3 -and
            $OriginalBytes[0] -eq 239 -and
            $OriginalBytes[1] -eq 187 -and
            $OriginalBytes[2] -eq 191)
$Original = [System.IO.File]::ReadAllText($Cycle)
$NL = if ($Original.Contains($CRLF)) { $CRLF } else { $LF }
$OldBlock = @'
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
'@
$NewBlock = @'
    . (Join-Path $RepoRoot "automation\network_probe.ps1")
    $NetworkReady = $false
    $NetworkDeadline = [DateTime]::UtcNow.AddSeconds(90)
    for ($i = 0; $i -lt 18; $i++) {
        $RemainingMs = [int]($NetworkDeadline - [DateTime]::UtcNow).TotalMilliseconds
        if ($RemainingMs -le 0) { break }
        $ProbeMs = [math]::Min(4000, $RemainingMs)
        if (Test-MiraiTcpPort -ComputerName "www.googleapis.com" -Port 443 -TimeoutMilliseconds $ProbeMs) {
            $NetworkReady = $true
            break
        }
        $RemainingMs = [int]($NetworkDeadline - [DateTime]::UtcNow).TotalMilliseconds
        if ($RemainingMs -gt 0) {
            Start-Sleep -Milliseconds ([math]::Min(5000, $RemainingMs))
        }
    }
'@
$OldBlock = $OldBlock.Replace($CRLF,$LF).Replace($LF,$NL)
$NewBlock = $NewBlock.Replace($CRLF,$LF).Replace($LF,$NL)
if ($Original.Contains($NewBlock)) { throw "PATCH_ALREADY_APPLIED_NO_CHANGES" }
if (-not $Original.Contains($OldBlock)) { throw "LOCAL_NETWORK_BLOCK_DIFFERS_ABORT" }
$First = $Original.IndexOf($OldBlock)
if ($Original.IndexOf($OldBlock, $First + 1) -ge 0) {
    throw "MULTIPLE_NETWORK_BLOCKS_ABORT"
}
$Patched = $Original.Replace($OldBlock, $NewBlock)
[scriptblock]::Create($Patched) | Out-Null
$Backup = $Cycle + ".backup-" + (Get-Date -Format "yyyyMMdd-HHmmss-ffff")
if (Test-Path -LiteralPath $Backup) { throw "BACKUP_PATH_CONFLICT" }
Copy-Item -LiteralPath $Cycle -Destination $Backup -ErrorAction Stop

$HelperCreated = $false
try {
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    if (-not (Test-Path -LiteralPath $Helper)) {
        [System.IO.File]::WriteAllText($Helper, $HelperText, $Utf8NoBom)
        $HelperCreated = $true
    }
    [System.IO.File]::WriteAllText(
        $Cycle, $Patched, ([System.Text.UTF8Encoding]::new($KeepBom))
    )
    Write-Host "NETWORK_PROBE_PATCHED=YES"
    Write-Host "ORIGINAL_SCRIPT_BACKUP=$Backup"
    Write-Host "LOCAL_OTHER_CHANGES=PRESERVED"
}
catch {
    Copy-Item -LiteralPath $Backup -Destination $Cycle -Force
    if ($HelperCreated) {
        Remove-Item -LiteralPath $Helper -Force -ErrorAction SilentlyContinue
    }
    throw
}
