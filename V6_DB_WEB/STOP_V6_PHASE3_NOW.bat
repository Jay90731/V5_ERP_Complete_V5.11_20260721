@echo off
setlocal
if not exist "%~dp0control" mkdir "%~dp0control"
echo stop>"%~dp0control\STOP_V6_NOW.flag"
echo 已提出停止要求；工具會在安全位置停止。
pause
endlocal
