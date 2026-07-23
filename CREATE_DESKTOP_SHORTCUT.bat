@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1"
pause
exit /b %ERRORLEVEL%

