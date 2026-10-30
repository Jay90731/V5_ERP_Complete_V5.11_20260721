@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BHQ_WATCHER_CONTROL.ps1" -Action Status
pause
