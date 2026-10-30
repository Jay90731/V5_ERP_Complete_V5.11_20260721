@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_phase3_environment.ps1"
set EXIT_CODE=%ERRORLEVEL%
echo.
if "%EXIT_CODE%"=="0" (
  echo 環境檢查通過，可以開始 ERP 測試。
) else (
  echo 環境尚未通過，請查看 environment_reports。
)
pause
exit /b %EXIT_CODE%
