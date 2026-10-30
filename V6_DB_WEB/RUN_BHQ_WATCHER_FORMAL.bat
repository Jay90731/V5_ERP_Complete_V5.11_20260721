@echo off
chcp 65001 >nul
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0RUN_BHQ_WATCHER_FORMAL.ps1"
exit /b %ERRORLEVEL%
