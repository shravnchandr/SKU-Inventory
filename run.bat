@echo off
REM Double-click this file (Windows) to start the inventory app.
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if not %errorlevel%==0 (
  echo Setup hasn't been run yet on this computer.
  echo Double-click install.bat first, then come back to this.
  pause
  exit /b 1
)

uv run app.py
pause
