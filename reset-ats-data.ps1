# Explicitly wipe local ATS business data after user confirmation.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "DELETE ALL ATS RESUMES AND JOB DESCRIPTIONS" -ForegroundColor Red
Write-Host "This permanently removes jobs, CVs, stored text, embeddings and scoring."
Write-Host "Keeps code, .env, database schema and Ollama models."
Write-Host ""
$confirmation = Read-Host 'Type DELETE ALL ATS DATA to confirm'
if ($confirmation -cne "DELETE ALL ATS DATA") {
    Write-Host "Cancelled; no data deleted."
    exit 0
}
if (-not (Test-Path ".venv/Scripts/python.exe")) {
    Write-Host "ATS environment not found. Run Start ATS.cmd first." -ForegroundColor Red
    exit 1
}

# Stop only this repository's detached ATS background worker.
$venvDir = Join-Path $PSScriptRoot ".venv"
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*job_hire_ai.workers.pipeline_worker*" -and
    $_.ExecutablePath -and
    $_.ExecutablePath.StartsWith(
        $venvDir, [System.StringComparison]::OrdinalIgnoreCase
    )
} | ForEach-Object {
    Write-Host "Stopping ATS pipeline worker..." -ForegroundColor Yellow
    Stop-Process -Id $_.ProcessId -Force
}

# ATS Settings reads the verified PostgreSQL connection from .env.
& ".venv/Scripts/python.exe" -m job_hire_ai.reset_ats_data --confirm $confirmation
if ($LASTEXITCODE -ne 0) {
    Write-Host "Reset did not complete. Read the error above." -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "ATS is empty. Restart with Start ATS.cmd." -ForegroundColor Green
