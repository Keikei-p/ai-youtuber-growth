$ErrorActionPreference = "Stop"
$candidates = @(
    (Get-Command tailscale.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:ProgramFiles\Tailscale\tailscale.exe",
    "$env:LOCALAPPDATA\Tailscale\tailscale.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
if (-not $candidates) { Write-Output "Tailscale未導入"; exit 0 }
$Tailscale = $candidates | Select-Object -First 1
& $Tailscale serve reset | Out-Host
Write-Output "Tailscale Serveを解除しました。"
