@echo off
rem Starts the subfix web UI (http://127.0.0.1:8765) and opens it in the browser.
rem Double-click this file, or use the "subfix" desktop shortcut. Close this window (or press
rem Ctrl+C) to stop the web UI. Extra arguments are passed on, e.g.  subfix-web.bat --port 8800
rem Set SUBFIX_WEB_DRYRUN=1 to only print the command that would be run.
setlocal
title subfix web UI

rem The repository is two folders up from this script (scripts\windows\).
for %%I in ("%~dp0..\..") do set "SUBFIX_REPO=%%~fI"
set "SUBFIX_EXE=%SUBFIX_REPO%\.venv\Scripts\subfix.exe"

if not exist "%SUBFIX_EXE%" (
    echo.
    echo  subfix is not installed yet: "%SUBFIX_EXE%" was not found.
    echo.
    echo  Set it up first ^(see the README in "%SUBFIX_REPO%"^), for example:
    echo      cd /d "%SUBFIX_REPO%"
    echo      py -3.13 -m venv .venv
    echo      .venv\Scripts\python -m pip install -e ".[gpu,dev]"
    echo.
    pause
    exit /b 1
)

rem Same effect as activating the virtual environment.
set "VIRTUAL_ENV=%SUBFIX_REPO%\.venv"
set "PATH=%SUBFIX_REPO%\.venv\Scripts;%PATH%"
cd /d "%SUBFIX_REPO%"

if "%SUBFIX_WEB_DRYRUN%"=="1" (
    echo DRY RUN: "%SUBFIX_EXE%" web %*
    echo working directory: "%CD%"
    exit /b 0
)

echo Starting the subfix web UI. Keep this window open while you use it; close it to stop.
echo.
"%SUBFIX_EXE%" web %*
set "SUBFIX_RC=%ERRORLEVEL%"
if not "%SUBFIX_RC%"=="0" (
    echo.
    echo  subfix web stopped with an error ^(exit code %SUBFIX_RC%^). See the messages above.
    echo.
    pause
)
exit /b %SUBFIX_RC%
