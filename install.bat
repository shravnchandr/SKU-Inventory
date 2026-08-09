@echo off
REM One-time setup (Windows). Double-click this file, then double-click
REM run.bat whenever you want to use the app.
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === Inventory app setup ===
echo.

where uv >nul 2>nul
if %errorlevel%==0 (
  echo uv is already installed.
  goto :sync
)

echo Installing uv (the tool that runs this app)...
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

REM The installer updates PATH for *future* terminal sessions. This window
REM won't see that change until it's reopened, so add the usual install
REM location to PATH for the rest of this script.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo uv installed, but this window can't find it yet. Close this window,
  echo open a new Command Prompt, and run this script again — that's usually enough.
  pause
  exit /b 1
)

:sync
echo.
echo Installing app dependencies...
uv sync
if not %errorlevel%==0 (
  echo.
  echo Something went wrong running "uv sync" — see the error above.
  pause
  exit /b 1
)

echo.
echo === Setup complete ===
echo Double-click run.bat any time you want to open the app.
pause
