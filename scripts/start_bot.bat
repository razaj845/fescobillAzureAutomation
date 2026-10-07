@echo off
REM For TESTING in a visible window. For everyday use run install_autostart.bat instead.
cd /d "%~dp0.."
python src\bot_listener.py
echo.
echo Bot stopped (exit code %errorlevel%).
pause
