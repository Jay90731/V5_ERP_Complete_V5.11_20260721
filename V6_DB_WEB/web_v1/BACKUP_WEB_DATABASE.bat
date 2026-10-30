@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PROJECT=%ROOT%.."
if exist "%ROOT%data\orders.db" set "PROJECT=%ROOT%"
set "PYTHON=%ROOT%runtime\python\python.exe"
if exist "%PROJECT%runtime\python\python.exe" set "PYTHON=%PROJECT%runtime\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" -X utf8 "%ROOT%backup_database.py" --database "%PROJECT%data\orders.db" --destination "%PROJECT%backups\formal"
pause
