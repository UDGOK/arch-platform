$headers = @{
    "Content-Type" = "application/json"
}

# Test dispatch to get a real job
$dispatchBody = @{
    project_name = "Test Project"
    building_type = "Commercial"
    occupancy_group = "B"
    construction_type = "Type II-A"
    jurisdiction_preset = "Chicago, IL"
    primary_code = "IBC 2021"
    drawing_sets = @("Floor Plan", "Exterior Elevations")
    engine_provider = "Mock Engine (Testing)"
    sprinklered = $true
} | ConvertTo-Json

$response = Invoke-WebRequest -Uri "https://arch-platform-psi.vercel.app/api/dispatch" -Method Post -Body $dispatchBody -Headers $headers -UseBasicParsing
$result = $response.Content | ConvertFrom-Json
Write-Host "Job ID: $($result.job_id)"

# Now add some mock image data to simulate what happens with real images
$result.drawings[0] | Add-Member -NotePropertyName "image_b64" -NotePropertyValue ("data:image/png;base64," + ("A" * 1000))
$result.drawings[0] | Add-Member -NotePropertyName "has_image" -NotePropertyValue $true

# Test export with large job
Write-Host "`nTesting export with image data..."
$exportBody = @{ job = $result } | ConvertTo-Json -Depth 10

try {
    $pdfResponse = Invoke-WebRequest -Uri "https://arch-platform-psi.vercel.app/api/export/pdf" -Method Post -Body $exportBody -Headers $headers -UseBasicParsing
    Write-Host "PDF Status: $($pdfResponse.StatusCode)"
    Write-Host "PDF Size: $($pdfResponse.Content.Length) bytes"
} catch {
    Write-Host "Error: $($_.Exception.Message)"
    if ($_.Exception.Response) {
        $statusCode = [int]$_.Exception.Response.StatusCode
        Write-Host "Status Code: $statusCode"
        $reader = [System.IO.StreamReader]::new($_.Exception.Response.GetResponseStream())
        $bodyText = $reader.ReadToEnd()
        $reader.Close()
        Write-Host "Response: $bodyText"
    }
}
