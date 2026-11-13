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

    # Existing PostgreSQL volumes retain the original password even when the
    # container environment changes. Always authenticate before saving secrets.
    $password = $config["ATS_POSTGRES_PASSWORD"]
    if ([string]::IsNullOrWhiteSpace($password) -and $config["ATS_DATABASE_URL_OVERRIDE"]) {
        try {
            $savedUri = [uri]($config["ATS_DATABASE_URL_OVERRIDE"] -replace '^postgresql\+psycopg:', 'postgresql:')
            if ($savedUri.UserInfo -match '^ats:(.*)$') {
                $password = [uri]::UnescapeDataString($matches[1])
            }
        } catch { }
    }
    $connected = $false
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        if ([string]::IsNullOrWhiteSpace($password) -or $attempt -gt 1) {
            Write-Host "Enter the ORIGINAL password for the existing ats PostgreSQL database." -ForegroundColor Yellow
            $secure = Read-Host "PostgreSQL password (not displayed)" -AsSecureString
            $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        }
        if ([string]::IsNullOrWhiteSpace($password)) { Fail "Password cannot be empty." }
        if ($password -match '[#$"\x60]' -or $password.Contains([char]10) -or $password.Contains([char]13)) {
            Fail "Password contains characters requiring manual .env escaping. Existing settings were not changed."
        }
        $encoded = [uri]::EscapeDataString($password)
        $dbUrl = "postgresql+psycopg://ats:${encoded}@127.0.0.1:5433/job_hire_ai"
        $env:ATS_POSTGRES_PASSWORD = $password
        $env:ATS_DATABASE_URL_OVERRIDE = $dbUrl
        Write-Host "Starting local PostgreSQL..." -ForegroundColor Cyan
        docker compose -f "compose.postgres.yml" up -d
        if ($LASTEXITCODE -ne 0) { Fail "PostgreSQL could not start. Existing data was not reset." }

        # A new Postgres volume needs time to initialize; a connection refusal
        # is not the same as an incorrect password.
        $oldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            uv run --no-sync python scripts/check_postgres.py 2>&1 | Out-Null
            $checkExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $oldPreference
        }
        if ($checkExit -eq 0) {
            $connected = $true
            break
        }
        if ($checkExit -ne 2) {
            Fail "PostgreSQL did not become reachable. Check Docker Desktop/container logs; do not change the password."
        }
        Write-Host "PostgreSQL rejected the stored password. Enter the database's actual password." -ForegroundColor Yellow
    }
    if (-not $connected) { Fail "Unable to authenticate to the existing PostgreSQL database. Nothing was overwritten." }

    # Only persist verified credentials. Subsequent launches require no password input.
    Set-Config $envPath "ATS_POSTGRES_PASSWORD" $password
    Set-Config $envPath "ATS_DATABASE_URL_OVERRIDE" $dbUrl
    Set-Config $envPath "ATS_EMBEDDINGS_ENABLED" "true"
    Set-Config $envPath "ATS_EMBEDDING_MODEL" "nomic-embed-text"
    Set-Config $envPath "ATS_EMBEDDING_BASE_URL" "http://127.0.0.1:11434"
    Set-Config $envPath "ATS_PROFILE_EXTRACTION_ENABLED" "true"
    Set-Config $envPath "ATS_EVIDENCE_VALIDATION_ENABLED" "true"
    Set-Config $envPath "ATS_MATCHING_ENABLED" "true"
    Set-Config $envPath "ATS_SCORING_ENABLED" "true"

    # Upgrade only known legacy defaults. Preserve customized model choices.
    $lightweightDefaults = @(
        @("OLLAMA_MODEL", "llama3.2:1b", "qwen2.5:0.5b"),
        @("OLLAMA_NUM_THREAD", "8", "2"),
        @("OLLAMA_TIMEOUT", "600", "120"),
        @("OLLAMA_RETRIES", "1", "0")
    )
    foreach ($item in $lightweightDefaults) {
        if (-not $config.ContainsKey($item[0]) -or $config[$item[0]] -eq $item[1]) {
            Set-Config $envPath $item[0] $item[2]
            [Environment]::SetEnvironmentVariable($item[0], $item[2], "Process")
        }
    }
    # Use exit codes, not native stderr, to detect whether the model is installed.
    # Under ErrorActionPreference=Stop, "ollama show" prints "model not found" to
    # stderr and otherwise aborts the entire PowerShell launcher before pull.
    $modelToUse = (Read-Config $envPath)["OLLAMA_MODEL"]
    $ollamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
    if ($modelToUse -eq "qwen2.5:0.5b" -and $ollamaCommand) {
        $ollamaExe = $ollamaCommand.Source
        $available = $false
        try {
            $check = Start-Process -FilePath $ollamaExe -ArgumentList @(
                "show", "qwen2.5:0.5b"
            ) -Wait -PassThru -NoNewWindow -RedirectStandardError (
                Join-Path $env:TEMP "ats-ollama-model-check.err"
            ) -RedirectStandardOutput (
                Join-Path $env:TEMP "ats-ollama-model-check.out"
            )
            $available = ($check.ExitCode -eq 0)
        } catch {
            Write-Host "Cannot check local model; will try the small model download." -ForegroundColor Yellow
        }
        if (-not $available) {
            Write-Host "Installing lightweight qwen2.5:0.5b model..." -ForegroundColor Cyan
            try {
                $pull = Start-Process -FilePath $ollamaExe -ArgumentList @(
                    "pull", "qwen2.5:0.5b"
                ) -Wait -PassThru -NoNewWindow
                $available = ($pull.ExitCode -eq 0)
            } catch {
                $available = $false
            }
        }
        if (-not $available) {
            Write-Host "Small model unavailable; trying installed llama3.2:1b instead." -ForegroundColor Yellow
            try {
                $legacy = Start-Process -FilePath $ollamaExe -ArgumentList @(
                    "show", "llama3.2:1b"
                ) -Wait -PassThru -NoNewWindow -RedirectStandardError (
                    Join-Path $env:TEMP "ats-ollama-legacy-check.err"
                ) -RedirectStandardOutput (
                    Join-Path $env:TEMP "ats-ollama-legacy-check.out"
                )
                if ($legacy.ExitCode -eq 0) {
                    Set-Config $envPath "OLLAMA_MODEL" "llama3.2:1b"
                    $env:OLLAMA_MODEL = "llama3.2:1b"
                    $available = $true
                }
            } catch {
                $available = $false
            }
        }
        if (-not $available) {
            Write-Host "No local CV language model found. ATS UI will start, but AI analysis requires an installed model." -ForegroundColor Yellow
        }
    } elseif ($modelToUse -eq "qwen2.5:0.5b") {
        Write-Host "Ollama CLI not found. ATS UI will start; install/start Ollama to use local AI features." -ForegroundColor Yellow
    }

    # Upgrade only unchanged legacy extraction defaults. Preserve any user-
    # customized model profile settings and the existing database/credentials.
    $cvDefaults = @(
        @("OLLAMA_CV_CHUNK_CHARS", "2400", "1200"),
        @("OLLAMA_CV_TRUNCATION_RETRIES", "2", "4"),
        @("OLLAMA_CV_MAX_MODEL_CALLS", "16", "32"),
        @("OLLAMA_CV_MAX_OUTPUT_TOKENS", "1536", "3072")
    )
    foreach ($item in $cvDefaults) {
        if ($config.ContainsKey($item[0]) -and $config[$item[0]] -eq $item[1]) {
            Set-Config $envPath $item[0] $item[2]
            [Environment]::SetEnvironmentVariable($item[0], $item[2], "Process")
        }
    }

    $env:ATS_EMBEDDINGS_ENABLED = "true"
    $env:ATS_EMBEDDING_MODEL = "nomic-embed-text"
    $env:ATS_EMBEDDING_BASE_URL = "http://127.0.0.1:11434"
    $env:ATS_PROFILE_EXTRACTION_ENABLED = "true"
    $env:ATS_EVIDENCE_VALIDATION_ENABLED = "true"
    $env:ATS_MATCHING_ENABLED = "true"
    $env:ATS_SCORING_ENABLED = "true"

    Write-Host "Checking database schema..." -ForegroundColor Cyan
    uv run --no-sync alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        Fail "Database migration/connection failed. Existing database was not reset."
    }

    # The background pipeline worker survives Streamlit restarts. Replace only
    # workers using this project's virtual environment so new pipeline code and
    # settings are loaded automatically. Startup recovery safely requeues
    # interrupted stages; unrelated Python processes are never touched.
    $venvDir = Join-Path $PSScriptRoot ".venv"
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*job_hire_ai.workers.pipeline_worker*" -and
        $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith(
            $venvDir, [System.StringComparison]::OrdinalIgnoreCase
        )
    } | ForEach-Object {
        Write-Host "Restarting existing ATS pipeline worker..." -ForegroundColor Cyan
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    }

    Write-Host "ATS ready. Open http://localhost:8501" -ForegroundColor Green
    Write-Host "Close with Ctrl+C. Worker starts automatically with Streamlit."
    uv run --no-sync streamlit run app.py
    if ($LASTEXITCODE -ne 0) { Fail "Streamlit exited with an error." }
} catch {
    Fail $_.Exception.Message
}
