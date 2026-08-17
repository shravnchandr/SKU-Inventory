@echo off
REM Double-click this file (Windows) to update the app to the latest version.
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

echo === Updating inventory app ===
echo.

where git >nul 2>nul
if not %errorlevel%==0 (
  echo git isn't installed yet — installing it now...
  where winget >nul 2>nul
  if not %errorlevel%==0 (
    echo.
    echo Couldn't install git automatically ^(winget isn't available on this computer^).
    echo Install it yourself from https://git-scm.com/download/win, then run this update again.
    pause
    exit /b 1
  )
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  REM The installer updates PATH for *future* sessions only — add its usual
  REM install location so the rest of this script can find it right away.
  set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
  where git >nul 2>nul
  if not %errorlevel%==0 (
    echo.
    echo git installed, but this window can't find it yet. Close this window, open a new
    echo Command Prompt, and run this script again — that's usually enough.
    pause
    exit /b 1
  )
  echo git installed.
)

if not exist ".git" (
  echo This folder isn't set up to receive updates automatically.
  echo Ask whoever set up this app to re-copy the latest version of the folder instead.
  pause
  exit /b 1
)

git remote get-url origin >nul 2>nul
if not %errorlevel%==0 (
  echo No update source is configured yet for this copy of the app.
  echo Ask whoever set up this app to configure one.
  pause
  exit /b 1
)

echo Checking for updates...
git fetch origin
if not %errorlevel%==0 (
  echo.
  echo Couldn't reach the update source — check your internet connection and try again.
  pause
  exit /b 1
)

set "BRANCH=main"
git rev-parse --verify origin/%BRANCH% >nul 2>nul
if not %errorlevel%==0 (
  echo The update source doesn't have the expected '%BRANCH%' branch. Ask whoever set up this app to check it.
  pause
  exit /b 1
)

for /f %%i in ('git rev-parse HEAD') do set "LOCAL=%%i"
for /f %%i in ('git rev-parse origin/%BRANCH%') do set "REMOTE=%%i"
if "%LOCAL%"=="%REMOTE%" (
  echo.
  echo Already up to date.
  pause
  exit /b 0
)

echo.
echo Updating to the latest version...
REM Matches the latest version exactly — this folder is only ever a deployed
REM copy, never edited by hand, so there's nothing in the tracked files worth
REM preserving. Your actual data (db\, uploads\, output\) is never tracked by
REM git, so none of it is touched by this, no matter what.
git reset --hard origin/%BRANCH%
if not %errorlevel%==0 (
  echo Something went wrong updating the code — see the error above.
  pause
  exit /b 1
)

echo.
where uv >nul 2>nul
if not %errorlevel%==0 (
  echo uv isn't installed yet — double-click install.bat first, then try updating again.
  pause
  exit /b 1
)

echo Installing any new dependencies...
uv sync
if not %errorlevel%==0 (
  echo Something went wrong installing dependencies — see the error above.
  pause
  exit /b 1
)

echo.
echo === Update complete ===
echo Double-click run.bat to use the app.
pause
