@echo off
setlocal
cd /d "%~dp0"

rem No PowerShell commands required for routine local upgrades.
rem Only fast-forward the intended ATS development branch; never overwrite
rem uncommitted work, local .env settings, uploads, or PostgreSQL volumes.
where git >nul 2>&1
if errorlevel 1 (
  echo Git is unavailable. ATS will use already installed source code.
) else (
  for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "ATS_BRANCH=%%B"
  if /I "%ATS_BRANCH%"=="feature/ats-pipeline" (
    echo Checking ATS updates...
    git pull --ff-only origin feature/ats-pipeline
    if errorlevel 1 (
      echo ATS update could not fast-forward. Local changes were not overwritten.
      echo Update with GitHub Desktop before launching ATS.
      pause
      exit /b 1
    )
  ) else (
    echo ATS current branch is not feature/ats-pipeline; skipping auto-update.
  )
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-ats.ps1"
if errorlevel 1 (
  echo.
  echo ATS did not start. Read the error above.
  pause
)
endlocal
