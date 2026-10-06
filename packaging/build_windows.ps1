param(
    [switch]$Studio,
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
    if (-not $PythonCommand) { throw "Python is unavailable." }
    $Python = $PythonCommand.Source
}
Write-Host "[PACKAGING] Python: $Python"

& $Python -m pip install -r requirements-packaging.txt
if ($LASTEXITCODE -ne 0) { throw "Packaging dependencies failed." }

if ($Studio) {
    Write-Host "[PACKAGING] Installing optional Studio Pack dependencies."
    & $Python -m pip install -r requirements-studio.txt
    if ($LASTEXITCODE -ne 0) { throw "Studio Pack dependencies failed." }
}

$Args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", "MiraiProductionOS",
    "--distpath", $OutputDir,
    "--workpath", "build\pyinstaller",
    "--specpath", "build\pyinstaller",
    "--add-data", "assets;assets",
    "--add-data", "character;character",
    "--add-data", "automation;automation",
    "--add-data", "docs;docs",
    "--add-data", "THIRD_PARTY_NOTICES.md;.",
    "--hidden-import", "googleapiclient.discovery",
    "--hidden-import", "google_auth_oauthlib.flow",
    "web_launcher.pyw"
)

if ($Studio) {
    $Args += @(
        "--hidden-import", "torch",
        "--collect-submodules", "diffusers",
        "--collect-submodules", "transformers"
    )
}

Write-Host "[PACKAGING] Building Mirai Production OS."
& $Python @Args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$Target = Join-Path $RepoRoot ($OutputDir + "\MiraiProductionOS")
if (-not (Test-Path $Target)) { throw "Build output not found: $Target" }

@"
Mirai Production OS - Beta

User data:
- Database, generated media, YouTube authentication, and .env are stored under
  %LOCALAPPDATA%\YOROKOBI\MiraiProductionOS in packaged Windows builds.
- Updating or reinstalling the application must not overwrite user data.

Before first use:
1. Put client_secret.json in the user-data directory when using YouTube.
2. Complete the first YouTube authorization.
3. Install Ollama / VOICEVOX / FFmpeg when using Local Studio features.
4. Never distribute API keys or tokens inside the application package.

Before commercial release:
- Code-sign the installer and binaries.
- Finalize Terms and Privacy Policy.
- Recheck all third-party commercial licenses.
"@ | Set-Content -Path (Join-Path $Target "RELEASE_NOTES.txt") -Encoding UTF8

Write-Host "[PACKAGING] Done: $Target"
