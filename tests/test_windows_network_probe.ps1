$ErrorActionPreference = "Stop"

# No external network use, no scheduled task, YouTube, or data mutations.
$Root = Split-Path -Parent $PSScriptRoot
$Probe = Join-Path $Root "automation\network_probe.ps1"
$Cycle = Join-Path $Root "automation\windows_cycle.ps1"

[scriptblock]::Create((Get-Content -Raw -LiteralPath $Probe)) | Out-Null
[scriptblock]::Create((Get-Content -Raw -LiteralPath $Cycle)) | Out-Null
. $Probe

$Listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$Listener.Start()
$Port = [int]$Listener.LocalEndpoint.Port
try {
    if (-not (Test-MiraiTcpPort -ComputerName "127.0.0.1" -Port $Port -TimeoutMilliseconds 1000)) {
        throw "Open loopback TCP port was not detected"
    }
}
finally {
    $Listener.Stop()
}

$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
if (Test-MiraiTcpPort -ComputerName "127.0.0.1" -Port $Port -TimeoutMilliseconds 1000) {
    throw "Closed loopback TCP port was falsely reported online"
}
$Stopwatch.Stop()
if ($Stopwatch.Elapsed.TotalSeconds -gt 5) {
    throw ("Closed TCP probe waited too long: " + $Stopwatch.Elapsed.TotalSeconds)
}

$Source = Get-Content -Raw -LiteralPath $Cycle
if ($Source -match '(?m)^\s*if\s*\(Test-NetConnection') {
    throw "Unbounded Test-NetConnection is still used by cycle"
}
if (-not $Source.Contains('$NetworkDeadline = [DateTime]::UtcNow.AddSeconds(90)')) {
    throw "Whole-cycle deadline is missing"
}
if (-not $Source.Contains('Test-MiraiTcpPort -ComputerName "www.googleapis.com"')) {
    throw "Bounded network probe is not wired to cycle"
}

Write-Host "WINDOWS_BOUNDED_NETWORK_PROBE=PASS"
