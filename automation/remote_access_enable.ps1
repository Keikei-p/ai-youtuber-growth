$ErrorActionPreference = "Stop"

$Target = "http://127.0.0.1:8765"
$candidates = @(
    (Get-Command tailscale.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:ProgramFiles\Tailscale\tailscale.exe",
    "$env:LOCALAPPDATA\Tailscale\tailscale.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

if (-not $candidates) {
    throw "Tailscaleが未導入です。PCとスマホにTailscaleを入れて同じアカウントへ接続してください。"
}

$Tailscale = $candidates | Select-Object -First 1
$statusRaw = & $Tailscale status --json 2>$null
if (-not $statusRaw) {
    throw "Tailscaleの状態を取得できません。Tailscaleへログインしてください。"
}

$status = $statusRaw | ConvertFrom-Json
if ($status.BackendState -ne "Running") {
    throw ("Tailscaleが接続状態ではありません: " + $status.BackendState)
}

& $Tailscale serve --bg --yes $Target | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale Serveの設定に失敗しました。"
}

$dns = $status.Self.DNSName
if ($dns) {
    $dns = $dns.TrimEnd(".")
    Write-Output ("REMOTE_URL=https://" + $dns)
} else {
    Write-Output "REMOTE_URL=tailnet内のTailscale Serve URLを使用してください。"
}
Write-Output "REMOTE_MODE=private-tailnet-only"
Write-Output "LOCAL_BACKEND=http://127.0.0.1:8765"
