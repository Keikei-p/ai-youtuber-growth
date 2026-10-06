param(
    [switch]$Studio,
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw ".venv がありません。先に通常セットアップを完了してください。"
}

& $Python -m pip install -r requirements-packaging.txt
if ($LASTEXITCODE -ne 0) { throw "packaging依存の導入に失敗しました。" }

if ($Studio) {
    Write-Host "[PACKAGING] Studio Pack依存を追加します。"
    & $Python -m pip install -r requirements-studio.txt
    if ($LASTEXITCODE -ne 0) { throw "Studio Pack依存の導入に失敗しました。" }
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

Write-Host "[PACKAGING] Mirai Production OS をビルドします。"
& $Python @Args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$Target = Join-Path $RepoRoot ($OutputDir + "\MiraiProductionOS")
if (-not (Test-Path $Target)) { throw "出力フォルダを確認できません: $Target" }

@"
Mirai Production OS - Beta

初回起動前:
1. client_secret.json をユーザー側で設定
2. YouTube初回認証
3. Local Studio利用時は Ollama / VOICEVOX / FFmpeg を準備
4. APIキーやtokenを配布物へ同梱しない

販売公開前:
- コード署名
- インストーラー化
- 自動更新署名
- 利用規約/プライバシーポリシー確定
- 第三者ライセンス最終確認
"@ | Set-Content -Path (Join-Path $Target "RELEASE_NOTES.txt") -Encoding UTF8

Write-Host "[PACKAGING] 完了: $Target"
