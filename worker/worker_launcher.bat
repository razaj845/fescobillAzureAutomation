@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM FESCO BILL WORKER
REM AUTO-RESTART LAUNCHER
REM ============================================================

set "PROJECT_DIR=C:\Users\RJM\Videos\fescobill"
set "WORKER_DIR=C:\Users\RJM\Videos\fescobill\worker"
set "WORKER_SCRIPT=C:\Users\RJM\Videos\fescobill\worker\worker.py"
set "PYTHON_EXE=C:\Users\RJM\Videos\fescobill\.venv\Scripts\python.exe"
set "LOG_DIR=C:\Users\RJM\Videos\fescobill\logs"
set "LAUNCHER_LOG=C:\Users\RJM\Videos\fescobill\logs\worker_launcher.log"

if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%" >nul 2>&1
)

cd /d "%PROJECT_DIR%"

:START_WORKER

echo [%date% %time%] FESCO worker launcher starting... >> "%LAUNCHER_LOG%"

if not exist "%PYTHON_EXE%" (
    echo [%date% %time%] ERROR: Python executable not found: %PYTHON_EXE% >> "%LAUNCHER_LOG%"
    timeout /t 60 /nobreak >nul
    goto START_WORKER
)

if not exist "%WORKER_SCRIPT%" (
    echo [%date% %time%] ERROR: worker.py not found: %WORKER_SCRIPT% >> "%LAUNCHER_LOG%"
    timeout /t 60 /nobreak >nul
    goto START_WORKER
)

set "PYTHONPATH=%PROJECT_DIR%;%WORKER_DIR%;%PYTHONPATH%"

echo [%date% %time%] Starting worker.py... >> "%LAUNCHER_LOG%"

"%PYTHON_EXE%" -u "%WORKER_SCRIPT%" >> "%LAUNCHER_LOG%" 2>&1

set "EXIT_CODE=%ERRORLEVEL%"

echo [%date% %time%] worker.py exited with code %EXIT_CODE%. >> "%LAUNCHER_LOG%"
echo [%date% %time%] Restarting in 5 seconds... >> "%LAUNCHER_LOG%"

timeout /t 5 /nobreak >nul

goto START_WORKER
