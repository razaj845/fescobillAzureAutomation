```bat
@echo off
setlocal

REM ============================================================
REM FESCO BILL WORKER - MAXIMUM RELIABILITY TASK
REM ============================================================

set "TASK_NAME=FESCO Bill Worker"
set "PROJECT_DIR=C:\Users\RJM\Videos\fescobill"
set "LAUNCHER=%PROJECT_DIR%\worker\worker_launcher.bat"

echo.
echo ============================================================
echo FESCO BILL WORKER - INSTALL / REPAIR
echo ============================================================
echo.

if not exist "%LAUNCHER%" (
    echo ERROR: Launcher not found:
    echo %LAUNCHER%
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM Remove existing task
REM ------------------------------------------------------------

echo Removing existing task...

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

REM ------------------------------------------------------------
REM Create task at Windows startup
REM ------------------------------------------------------------

echo Creating SYSTEM startup task...

schtasks /create ^
 /tn "%TASK_NAME%" ^
 /tr "\"%LAUNCHER%\"" ^
 /sc onstart ^
 /ru SYSTEM ^
 /rl HIGHEST ^
 /f

if errorlevel 1 (
    echo.
    echo ERROR: Task creation failed.
    echo.
    echo Run this file as Administrator.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM Configure maximum reliability
REM ------------------------------------------------------------

echo.
echo Applying reliability settings...

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
 "$t = Get-ScheduledTask -TaskName '%TASK_NAME%';" ^
 "$t.Settings.ExecutionTimeLimit = 'PT0S';" ^
 "$t.Settings.IdleSettings.StopOnIdleEnd = $false;" ^
 "$t.Settings.IdleSettings.RestartOnIdle = $false;" ^
 "$t.Settings.DisallowStartIfOnBatteries = $false;" ^
 "$t.Settings.StopIfGoingOnBatteries = $false;" ^
 "$t.Settings.MultipleInstances = 'IgnoreNew';" ^
 "$t.Settings.RestartCount = 999999;" ^
 "$t.Settings.RestartInterval = 'PT1M';" ^
 "$t | Set-ScheduledTask"

if errorlevel 1 (
    echo.
    echo WARNING: Some reliability settings could not be applied.
)

REM ------------------------------------------------------------
REM Start immediately
REM ------------------------------------------------------------

echo.
echo Starting worker...

schtasks /run /tn "%TASK_NAME%"

if errorlevel 1 (
    echo.
    echo WARNING: Could not start immediately.
)

echo.
echo ============================================================
echo INSTALLATION COMPLETE
echo ============================================================
echo.
echo Worker:
echo   %PROJECT_DIR%\worker\worker.py
echo.
echo Startup:
echo   Windows boot - before login
echo.
echo Account:
echo   SYSTEM
echo.
echo Battery:
echo   Worker continues on battery
echo.
echo Execution limit:
echo   None
echo.
echo ============================================================
echo.

pause
```
