@echo off
chcp 65001 >nul
echo 請先關閉婚約訂單網站。
set /p "BACKUP=請輸入要還原的 .db 完整路徑："
set "ROOT=%~dp0"
set "PROJECT=%ROOT%.."
if exist "%ROOT%data\orders.db" set "PROJECT=%ROOT%"
set "PYTHON=%ROOT%runtime\python\python.exe"
if exist "%PROJECT%runtime\python\python.exe" set "PYTHON=%PROJECT%runtime\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" -X utf8 "%ROOT%restore_database.py" --database "%PROJECT%data\orders.db" --backup "%BACKUP%" --confirm RESTORE
pause
