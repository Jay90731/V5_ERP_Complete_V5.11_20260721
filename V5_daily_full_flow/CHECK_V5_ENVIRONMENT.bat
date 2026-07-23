@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_v5_environment.ps1"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" echo READY FOR ERP DAILY RUN
if "%RC%"=="1" echo MISSING REQUIRED OCR OR HISTORY ENVIRONMENT
if "%RC%"=="2" echo LOCAL REPLAY READY, ERP EXE NOT FOUND ON THIS COMPUTER
pause
exit /b %RC%

