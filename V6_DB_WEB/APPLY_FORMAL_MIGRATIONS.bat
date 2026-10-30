@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0formal_database_tools.ps1" -Action Migrate
pause
