$content = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Raw

# Replace double closing parens with single
$content = $content -replace "\)\s*\n\s*\)\s*\n", ")" + [Environment]::NewLine + [Environment]::NewLine

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $content -NoNewline
Write-Host "Fixed duplicate closing parens"
