@echo off
REM install_worker.bat — Run ONCE on each PC after filling in worker\worker.env
setlocal EnableDelayedExpansion

cd /d "%~dp0.."
set "PROJECT=%CD%"
set "WORKER_SCRIPT=%PROJECT%\worker\worker.py"
set "WORKER_ENV=%PROJECT%\worker\worker.env"

echo ═══════════════════════════════════════════════════════════
echo   FESCO Bill Worker -- PC Setup
echo   Project: %PROJECT%
echo ═══════════════════════════════════════════════════════════
echo.

if not exist "%WORKER_ENV%" (
    echo ERROR: worker\worker.env not found.
    echo   Copy worker\worker.env.example to worker\worker.env
    echo   then fill in AZURE_STORAGE_CONNECTION_STRING and WORKER_NAME.
    echo.
    pause
    exit /b 1
)

set "PYTHON=python"
if exist "%PROJECT%\.venv\Scripts\python.exe" (
    set "PYTHON=%PROJECT%\.venv\Scripts\python.exe"
    echo Using virtual environment: !PYTHON!
) else (
    echo Using system Python
)

echo.
echo [1/3] Installing Azure Storage packages...
"%PYTHON%" -m pip install --quiet azure-storage-queue==12.12.0 azure-data-tables==12.5.0
if %errorlevel% neq 0 ( echo ERROR: pip install failed. & pause & exit /b 1 )
echo   Done.

echo.
echo [2/3] Creating scheduled task "FescoBillWorker"...
schtasks /delete /tn "FescoBillWorker" /f >nul 2>&1
schtasks /create /tn "FescoBillWorker" /tr "\"%PYTHON%\" \"%WORKER_SCRIPT%\"" /sc ONLOGON /delay 0002:00 /ru "%USERDOMAIN%\%USERNAME%" /rl HIGHEST /f
if %errorlevel% neq 0 ( echo ERROR: Run as Administrator. & pause & exit /b 1 )
echo   Task created. Worker starts 2 min after every login.

echo.
echo [3/3] Starting worker now...
start "" /min "%PYTHON%" "%WORKER_SCRIPT%"

echo.
echo   Done. Check logs\worker.log to confirm it connected.
echo   On Telegram you will see: "Worker 'PC-Office' came online."
echo.
pause
