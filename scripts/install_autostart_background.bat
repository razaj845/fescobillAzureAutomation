@echo off
REM Same as install_autostart.bat but the bot ALSO starts at boot, before anybody logs in.
REM Right-click this file and choose "Run as administrator". You will be asked for your Windows password.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -RunWithoutLogin -KeepAwake
echo.
pause
