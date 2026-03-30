$lines = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py"

$newLines = @()
for ($i = 0; $i -lt $lines.Count; $i++) {
    $line = $lines[$i]
    $newLines += $line
    
    # Find the incomplete return statement and add closing paren
    if ($line.Trim() -eq "additional_notes=req.additional_notes,") {
        # Check if next line is blank
        if ($i + 1 -lt $lines.Count -and $lines[$i+1].Trim() -eq "") {
            # Replace blank line with closing paren and blank line
            $newLines[$newLines.Count - 1] = "        additional_notes=req.additional_notes,"
            $newLines += "    )"
            $newLines += ""
            $i++  # Skip the original blank line
            Write-Host "Fixed _build_spec closing paren"
        }
    }
}

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $newLines
Write-Host "Done"
