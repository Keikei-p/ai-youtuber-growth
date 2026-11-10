$ErrorActionPreference = "Stop"

# Integration test: native Python output and PowerShell messages share one
# UTF-8 log on Windows PowerShell 5.1. No scheduler or YouTube calls.
$dir = Join-Path $env:TEMP ("mirai-utf8-log-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$LogFile = Join-Path $dir "automation-test-utf8.log"
try {
    [System.IO.File]::WriteAllBytes($LogFile, [byte[]]@(239, 187, 191))
    $PSDefaultParameterValues["Out-File:Encoding"] = "utf8"
    $env:PYTHONIOENCODING = "utf-8"
    try {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    }
    catch {}

    Add-Content -Path $LogFile -Encoding UTF8 -Value "日本語追加確認"
    & python -c 'print("Python日本語出力")' *>> $LogFile
    if ($LASTEXITCODE -ne 0) { throw "Python log probe failed" }
    Add-Content -Path $LogFile -Encoding UTF8 -Value "追記確認"

    $text = Get-Content -LiteralPath $LogFile -Raw -Encoding UTF8
    foreach ($required in @("日本語追加確認", "Python日本語出力", "追記確認")) {
        if (-not $text.Contains($required)) {
            throw ("UTF-8 log verification failed for: " + $required)
        }
    }
    Write-Host "WINDOWS_LOG_UTF8=PASS"
}
finally {
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
}
