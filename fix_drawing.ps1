$lines = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py"

$newLines = @()
for ($i = 0; $i -lt $lines.Count; $i++) {
    $line = $lines[$i]
    
    # Fix missing closing paren in DrawingOutput
    if ($line.Trim() -eq 'metadata={"key_notes": req.key_notes, "drawing_prompt": req.drawing_prompt},') {
        $newLines += $line
        $newLines += "    )"
        Write-Host "Fixed DrawingOutput closing paren"
        continue
    }
    
    $newLines += $line
}

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $newLines
Write-Host "Done"
