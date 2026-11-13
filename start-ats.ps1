# One-time setup, then start ATS with a single double-click.
# Store local secrets in the git-ignored .env file, never in source control.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail([string]$message) {
    Write-Host "ATS startup failed: $message" -ForegroundColor Red
    exit 1
}

function Read-Config([string]$file) {
    $settings = @{}
    if (Test-Path $file) {
        foreach ($line in (Get-Content -LiteralPath $file)) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
                $settings[$matches[1]] = $matches[2].Trim().Trim('"', "'")
            }
        }
    }
    return $settings
}

function Set-Config([string]$file, [string]$key, [string]$value) {
    $lines = @()
    if (Test-Path $file) {
        $lines = @(Get-Content -LiteralPath $file)
    }
    $updated = $false
    $result = @()
    foreach ($line in $lines) {
        if ($line -match ('^\s*' + [regex]::Escape($key) + '\s*=')) {
            if (-not $updated) {
                $result += "$key=$value"
                $updated = $true
            }
        } else {
            $result += $line
        }
    }
    if (-not $updated) { $result += "$key=$value" }
    [System.IO.File]::WriteAllLines($file, [string[]]$result)
}

try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv is not installed or not in PATH."
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "Docker Desktop is missing. Install/start Docker Desktop first."
    }
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Fail "Docker Desktop is not running. Start it, then open Start ATS.cmd again."
    }

    $envPath = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path $envPath)) {
        Copy-Item (Join-Path $PSScriptRoot ".env.example") $envPath
    }
    $config = Read-Config $envPath
    $password = $config["ATS_POSTGRES_PASSWORD"]
    if ([string]::IsNullOrWhiteSpace($password)) {
        Write-Host "One-time PostgreSQL setup. This password stays in the local .env file."
        $secure = Read-Host "Enter existing PostgreSQL password" -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        }
        if ([string]::IsNullOrWhiteSpace($password)) {
            Fail "PostgreSQL password cannot be empty."
        }
        if ($password -match '[\r\n]') { Fail "Password must be one line." }
        # .env syntax supports standard simple passwords; refuse characters that Compose
        # might expand or parse differently to avoid accidentally changing DB credentials.
        if ($password -match '[\$#"`\r\n]') {
            Fail 'Password contains characters requiring special .env escaping. Configure .env manually.'
        }
        Set-Config $envPath "ATS_POSTGRES_PASSWORD" $password
    }
    $encoded = [uri]::EscapeDataString($password)
    $dbUrl = "postgresql+psycopg://ats:${encoded}@127.0.0.1:5433/job_hire_ai"
    Set-Config $envPath "ATS_DATABASE_URL_OVERRIDE" $dbUrl
    Set-Config $envPath "ATS_EMBEDDINGS_ENABLED" "true"
    Set-Config $envPath "ATS_EMBEDDING_MODEL" "nomic-embed-text"
    Set-Config $envPath "ATS_EMBEDDING_BASE_URL" "http://127.0.0.1:11434"

    # Explicit process values override stale variables from an old PowerShell window.
    $env:ATS_POSTGRES_PASSWORD = $password
    $env:ATS_DATABASE_URL_OVERRIDE = $dbUrl
    $env:ATS_EMBEDDINGS_ENABLED = "true"
    $env:ATS_EMBEDDING_MODEL = "nomic-embed-text"
    $env:ATS_EMBEDDING_BASE_URL = "http://127.0.0.1:11434"

    Write-Host "Starting local PostgreSQL..." -ForegroundColor Cyan
    docker compose --env-file ".env" -f "compose.postgres.yml" up -d
    if ($LASTEXITCODE -ne 0) { Fail "PostgreSQL could not start. Check Docker Desktop." }

    if (-not (Test-Path ".venv")) {
        Write-Host "Installing ATS dependencies (first launch only)..."
        uv sync --frozen --python 3.12
        if ($LASTEXITCODE -ne 0) { Fail "Dependency installation failed." }
    }
    uv run --no-sync python -c "import psycopg" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing PostgreSQL driver (first launch only)..."
        uv pip install --python .venv -r requirements-postgres.txt
        if ($LASTEXITCODE -ne 0) { Fail "PostgreSQL driver installation failed." }
    }

    Write-Host "Checking database schema..." -ForegroundColor Cyan
    uv run --no-sync alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        Fail "Database migration/connection failed. The existing database was not reset."
    }

    Write-Host "ATS ready. Open http://localhost:8501" -ForegroundColor Green
    Write-Host "Close with Ctrl+C. Worker starts automatically with Streamlit."
    uv run --no-sync streamlit run app.py
    if ($LASTEXITCODE -ne 0) { Fail "Streamlit exited with an error." }
} catch {
    Fail $_.Exception.Message
}
