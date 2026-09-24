$TaskName = "AI YouTuber Growth"
& schtasks.exe /Delete /F /TN $TaskName | Out-Host
Write-Host "[OK] 自動化タスクを削除しました。"
