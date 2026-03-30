$lines = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py"

$newLines = @()
for ($i = 0; $i -lt $lines.Count; $i++) {
    $line = $lines[$i]
    $trimmed = $line.Trim()
    
    # Check for FastAPIResponse with headers that don't end with )
    if ($trimmed -match 'headers=\{"Content-Disposition":' -and $trimmed -notmatch '\),?\s*$') {
        $newLines += $line
        $newLines += "    )"
        Write-Host "Fixed FastAPIResponse closing paren"
        continue
    }
    
    $newLines += $line
}

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $newLines
Write-Host "Done"
