@echo off
chcp 65001 >nul
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo ERROR: virtualenv not found at .venv
    echo Run: uv sync
    pause
    exit /b 1
)

set "MODE=remote"
if /i "%~1"=="tui" set "MODE=tui"
if /i "%~1"=="remote" set "MODE=remote"

if "%MODE%"=="tui" (
    echo Starting Coding Agent - Terminal UI
    "%PY%" -m coding_agent
    goto :end
)

if "%MODE%"=="remote" (
    echo Starting Coding Agent - Remote UI
    echo Server URL: http://127.0.0.1:18888
    start "" "%PY%" -m coding_agent --remote
    timeout /t 4 /nobreak >nul
    start "" http://127.0.0.1:18888
    goto :end
)

echo Usage: start.bat [tui|remote]
:end
endlocal
