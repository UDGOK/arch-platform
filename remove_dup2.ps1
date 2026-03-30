$content = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Raw

# Replace patterns with double closing parens followed by newline
$pattern = "	 )" + [Environment]::NewLine + "	 )"
$replacement = "	 )" + [Environment]::NewLine
$content = $content -replace [regex]::Escape($pattern), $replacement

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $content -NoNewline
Write-Host "Fixed"
