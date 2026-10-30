@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHON=%~dp0runtime\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
"%PYTHON%" -X utf8 "%~dp0bhq_watch.py" --config "%~dp0bhq_watcher_test.json"
exit /b %ERRORLEVEL%
