@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PROJECT=%ROOT%.."
if exist "%ROOT%data\orders.db" set "PROJECT=%ROOT%"
set "PYTHON=%ROOT%runtime\python\python.exe"
if exist "%PROJECT%runtime\python\python.exe" set "PYTHON=%PROJECT%runtime\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONPATH=%PROJECT%src;%ROOT%"
"%PYTHON%" -X utf8 -m unittest discover -s "%ROOT%tests" -v
pause
