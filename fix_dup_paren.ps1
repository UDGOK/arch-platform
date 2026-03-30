$lines = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py"

$newLines = @()
$prevLine = ""
for ($i = 0; $i -lt $lines.Count; $i++) {
    $line = $lines[$i]
    $trimmed = $line.Trim()
    
    # Skip duplicate closing parens that appear after a closing paren line
    if ($trimmed -eq ")" -and ($prevLine.Trim() -eq ")" -or $prevLine -match '\)\s*$')) {
        Write-Host "Skipping duplicate closing paren"
        $prevLine = $line
        continue
    }
    
    $newLines += $line
    $prevLine = $line
}

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $newLines
Write-Host "Done"
