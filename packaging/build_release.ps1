param(
    [string]$Version = "",
    [string]$OutputDir = "dist\release",
    [switch]$Studio
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not $Version) {
    $Version = python -c "from product_core import PRODUCT_VERSION; print(PRODUCT_VERSION)"
    if ($LASTEXITCODE -ne 0) { throw "製品バージョン取得に失敗しました。" }
    $Version = $Version.Trim()
}

$ReleaseDir = Join-Path $RepoRoot $OutputDir
$BuildDir = Join-Path $RepoRoot "dist\windows"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }

Write-Host "[RELEASE] Windows本体をビルド: $Version"
$BuildArgs = @("-OutputDir", $BuildDir)
if ($Studio) { $BuildArgs += "-Studio" }
& (Join-Path $PSScriptRoot "build_windows.ps1") @BuildArgs
if ($LASTEXITCODE -ne 0) { throw "Windows本体ビルドに失敗しました。" }

$SourceDir = Join-Path $BuildDir "MiraiProductionOS"
if (-not (Test-Path (Join-Path $SourceDir "MiraiProductionOS.exe"))) {
    throw "MiraiProductionOS.exeを確認できません。"
}

$Portable = Join-Path $ReleaseDir ("MiraiProductionOS-" + $Version + "-Portable.zip")
if (Test-Path $Portable) { Remove-Item -Force $Portable }
Compress-Archive -Path (Join-Path $SourceDir "*") -DestinationPath $Portable -CompressionLevel Optimal

$MakeNsis = Get-Command makensis.exe -ErrorAction SilentlyContinue
if (-not $MakeNsis) {
    $Candidates = @(
        "C:\Program Files (x86)\NSIS\makensis.exe",
        "C:\Program Files\NSIS\makensis.exe"
    )
    foreach ($candidate in $Candidates) {
        if (Test-Path $candidate) { $MakeNsis = Get-Item $candidate; break }
    }
}
if (-not $MakeNsis) {
    throw "NSIS makensis.exe が見つかりません。NSIS 3.xを導入してください。"
}

$InstallerScript = Join-Path $PSScriptRoot "MiraiProductionOS.nsi"
& $MakeNsis.Source `
    "/DPRODUCT_VERSION=$Version" `
    "/DSOURCE_DIR=$SourceDir" `
    "/DOUTPUT_DIR=$ReleaseDir" `
    $InstallerScript
if ($LASTEXITCODE -ne 0) { throw "NSIS installer build failed." }

$Installer = Join-Path $ReleaseDir ("MiraiProductionOS-" + $Version + "-Setup.exe")
if (-not (Test-Path $Installer)) { throw "Installerを確認できません: $Installer" }

$Artifacts = @($Installer, $Portable)
$Manifest = @{}
foreach ($Artifact in $Artifacts) {
    $Hash = Get-FileHash -Algorithm SHA256 -Path $Artifact
    $Name = Split-Path -Leaf $Artifact
    $Manifest[$Name] = @{
        sha256 = $Hash.Hash.ToLowerInvariant()
        bytes = (Get-Item $Artifact).Length
    }
    ($Hash.Hash.ToLowerInvariant() + "  " + $Name) | Set-Content -Encoding ascii -Path ($Artifact + ".sha256")
}

$ManifestPayload = @{
    product = "Mirai Production OS"
    version = $Version
    channel = "beta"
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    signed = $false
    artifacts = $Manifest
}
$ManifestPath = Join-Path $ReleaseDir "release-manifest.json"
$ManifestPayload | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -Path $ManifestPath

Write-Host "[RELEASE] 完了"
Write-Host "  Installer: $Installer"
Write-Host "  Portable : $Portable"
Write-Host "  Manifest : $ManifestPath"
Write-Warning "コード署名は未実施です。正式販売前に署名証明書を設定してください。"
