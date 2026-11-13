@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-ats.ps1"
if errorlevel 1 (
  echo.
  echo ATS did not start. Read the error above.
  pause
)
endlocal
