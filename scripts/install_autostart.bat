@echo off
REM Creates the "FESCO Bill Bot" scheduled task (runs hidden, restarts itself, never stops on battery/idle/time limit).
REM Extra option:  install_autostart.bat -KeepAwake   (also stops the PC sleeping while plugged in)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
echo.
pause
