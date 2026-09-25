$ErrorActionPreference = "SilentlyContinue"
$candidates = @(
    (Get-Command tailscale.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:ProgramFiles\Tailscale\tailscale.exe",
    "$env:LOCALAPPDATA\Tailscale\tailscale.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
if (-not $candidates) { Write-Output "NOT_INSTALLED"; exit 0 }
$Tailscale = $candidates | Select-Object -First 1
$raw = & $Tailscale status --json 2>$null
if (-not $raw) { Write-Output "NOT_CONNECTED"; exit 0 }
$s = $raw | ConvertFrom-Json
if ($s.BackendState -ne "Running") { Write-Output ("STATE=" + $s.BackendState); exit 0 }
$dns = $s.Self.DNSName
if ($dns) { $dns = $dns.TrimEnd("."); Write-Output ("CONNECTED=https://" + $dns) } else { Write-Output "CONNECTED" }
& $Tailscale serve status 2>$null | Out-Host
