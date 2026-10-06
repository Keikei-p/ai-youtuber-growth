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
    if ($LASTEXITCODE -ne 0) { throw "Product version lookup failed." }
    $Version = $Version.Trim()
}

$ReleaseDir = Join-Path $RepoRoot $OutputDir
$BuildDir = Join-Path $RepoRoot "dist\windows"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }

Write-Host "[RELEASE] Building Windows app: $Version"
$BuildScript = Join-Path $PSScriptRoot "build_windows.ps1"
if ($Studio) {
    & $BuildScript -OutputDir $BuildDir -Studio
} else {
    & $BuildScript -OutputDir $BuildDir
}
if ($LASTEXITCODE -ne 0) { throw "Windows app build failed." }

$SourceDir = Join-Path $BuildDir "MiraiProductionOS"
$Exe = Join-Path $SourceDir "MiraiProductionOS.exe"
if (-not (Test-Path $Exe)) { throw "MiraiProductionOS.exe was not produced." }

$Portable = Join-Path $ReleaseDir ("MiraiProductionOS-" + $Version + "-Portable.zip")
if (Test-Path $Portable) { Remove-Item -Force $Portable }
Compress-Archive -Path (Join-Path $SourceDir "*") -DestinationPath $Portable -CompressionLevel Optimal

$MakeNsisCommand = Get-Command makensis.exe -ErrorAction SilentlyContinue
$MakeNsisPath = ""
if ($MakeNsisCommand) {
    $MakeNsisPath = $MakeNsisCommand.Source
}
if (-not $MakeNsisPath) {
    $Candidates = @(
        "C:\Program Files (x86)\NSIS\makensis.exe",
        "C:\Program Files\NSIS\makensis.exe"
    )
    foreach ($candidate in $Candidates) {
        if (Test-Path $candidate) {
            $MakeNsisPath = $candidate
            break
        }
    }
}
if (-not $MakeNsisPath) { throw "NSIS makensis.exe is unavailable." }

$InstallerScript = Join-Path $PSScriptRoot "MiraiProductionOS.nsi"
Write-Host "[RELEASE] NSIS: $MakeNsisPath"
& $MakeNsisPath `
    "/DPRODUCT_VERSION=$Version" `
    "/DSOURCE_DIR=$SourceDir" `
    "/DOUTPUT_DIR=$ReleaseDir" `
    $InstallerScript
if ($LASTEXITCODE -ne 0) { throw "NSIS installer build failed." }

$Installer = Join-Path $ReleaseDir ("MiraiProductionOS-" + $Version + "-Setup.exe")
if (-not (Test-Path $Installer)) { throw "Installer was not produced: $Installer" }

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

Write-Host "[RELEASE] Done."
Write-Host "  Installer: $Installer"
Write-Host "  Portable : $Portable"
Write-Host "  Manifest : $ManifestPath"
Write-Warning "Code signing is not configured yet. Do not treat this as a signed commercial release."
