$lines = Get-Content "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py"

$newLines = @()
for ($i = 0; $i -lt $lines.Count; $i++) {
    $line = $lines[$i]
    
    # Fix missing closing paren in FastAPIResponse for PDF export
    if ($line -match 'headers=\{"Content-Disposition":.*_documents.pdf"') {
        $newLines += $line
        $newLines += "    )"
        Write-Host "Fixed FastAPIResponse closing paren at PDF export"
        continue
    }
    
    $newLines += $line
}

Set-Content -Path "C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py" -Value $newLines
Write-Host "Done"
